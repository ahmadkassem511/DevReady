"""Detect a project's *own* canonical setup method.

Many repos don't expect you to run pip/npm directly — they ship a ``make
setup``, a ``setup.sh``, a Taskfile, or a Justfile that is the intended,
authoritative way to get the project ready. This module detects those so
DevReady can offer to run the project's own setup instead of guessing.

Design notes for contributors:
  * Detection is read-only. Whether anything actually runs is decided by the
    engine, which always asks the user first (we never execute repo-provided
    scripts without consent).
  * A strategy is only *offered* if the tool that runs it exists on the system
    (``make``/``task``/``just``/``bash``). Use :func:`available_strategies` for
    the runnable subset and :func:`detect_setup_strategies` for everything
    detected (so the engine can explain "this needs make, which isn't installed").
  * To add a new setup system: detect its file, find a setup-ish target, and
    append a :class:`SetupStrategy`. Keep the target priority conservative —
    prefer ``setup``/``bootstrap``/``init`` over ambiguous ones like ``install``
    (which on some projects means a system-wide install).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..utils import command_exists

# Setup-ish target names we look for, in priority order. ``setup`` first because
# it's the least ambiguous; ``install`` last because for some tools it means a
# system-wide install rather than "install this project's deps".
SETUP_TARGETS = ("setup", "bootstrap", "init", "dev", "install")


@dataclass
class SetupStrategy:
    """A concrete way to set the project up, as declared by the project itself.

    Attributes:
        name: Strategy kind — "makefile", "taskfile", "justfile", or "script".
        command: The argv to run (e.g. ``["make", "setup"]``).
        display: Human-readable form for prompts (e.g. ``"make setup"``).
        runner: The executable that must exist to run it (e.g. ``"make"``).
    """

    name: str
    command: List[str]
    display: str
    runner: str


def _first_existing(project_dir: Path, names) -> Optional[Path]:
    for name in names:
        path = project_dir / name
        if path.exists():
            return path
    return None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _pick_target(available, targets=SETUP_TARGETS) -> Optional[str]:
    """Return the highest-priority setup target present in ``available``."""
    for target in targets:
        if target in available:
            return target
    return None


def detect_setup_strategies(project_dir: Path) -> List[SetupStrategy]:
    """Detect all project-declared setup methods, best/most-common first."""
    found: List[SetupStrategy] = []

    # Makefile — targets look like ``setup:`` at the start of a line.
    makefile = _first_existing(project_dir, ("Makefile", "makefile", "GNUmakefile"))
    if makefile:
        targets = set(re.findall(r"^([a-zA-Z0-9_.-]+):", _read(makefile), re.MULTILINE))
        target = _pick_target(targets)
        if target:
            found.append(SetupStrategy("makefile", ["make", target], f"make {target}", "make"))

    # Justfile — recipe names look like ``setup:`` too.
    justfile = _first_existing(project_dir, ("Justfile", "justfile", ".justfile"))
    if justfile:
        recipes = set(re.findall(r"^([a-zA-Z0-9_-]+)\s*:", _read(justfile), re.MULTILINE))
        target = _pick_target(recipes)
        if target:
            found.append(SetupStrategy("justfile", ["just", target], f"just {target}", "just"))

    # Taskfile (go-task) — task names are 2-space-indented keys under ``tasks:``.
    taskfile = _first_existing(project_dir, ("Taskfile.yml", "Taskfile.yaml", "taskfile.yml"))
    if taskfile:
        tasks = set(re.findall(r"^\s{2}([a-zA-Z0-9_-]+):", _read(taskfile), re.MULTILINE))
        target = _pick_target(tasks)
        if target:
            found.append(SetupStrategy("taskfile", ["task", target], f"task {target}", "task"))

    # Setup scripts — run with bash. First match wins.
    script = _first_existing(
        project_dir,
        (
            "setup.sh", "install.sh", "bootstrap.sh",
            "scripts/setup.sh", "scripts/setup",
            "scripts/install.sh", "scripts/bootstrap.sh",
        ),
    )
    if script:
        rel = script.relative_to(project_dir).as_posix()
        found.append(SetupStrategy("script", ["bash", rel], f"bash {rel}", "bash"))

    return found


def available_strategies(project_dir: Path) -> List[SetupStrategy]:
    """Detected strategies whose runner is actually installed on this machine."""
    return [s for s in detect_setup_strategies(project_dir) if command_exists(s.runner)]
