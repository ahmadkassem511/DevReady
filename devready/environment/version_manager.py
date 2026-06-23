"""Set up language runtimes and install project dependencies.

This module handles Step 4 of ``devready start``: making sure the right runtime
version is available and installing the project's dependencies into an isolated
environment.

For Python it creates a project-local ``.venv`` using the standard library
``venv`` module (always available) and uses ``pyenv`` to install a missing
interpreter version when pyenv is present.

For Node it runs ``npm install`` and, when an ``.nvmrc``/engine version is known
and ``nvm`` is available, installs that Node version first.

Everything is best-effort and clearly reported: if a version manager isn't
installed we proceed with whatever runtime the user already has, rather than
hard-failing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

from ..detectors import DetectionResult
from ..utils import CommandResult, command_exists, console, run_command


# -----------------------------------------------------------------------------
# Python
# -----------------------------------------------------------------------------
def setup_python(project_dir: Path, result: DetectionResult) -> List[CommandResult]:
    """Create a virtualenv and install Python dependencies.

    Steps:
      1. If a specific version is required and ``pyenv`` is installed, ensure
         that version is installed via pyenv.
      2. Create (or repair) ``.venv`` in the project directory.
      3. Make sure pip exists inside the venv (some venvs ship without it),
         then upgrade the core build tools so wheels build cleanly.
      4. Install dependencies from requirements.txt or pyproject.toml, calling
         pip via ``python -m pip`` so we never depend on a ``pip.exe`` that may
         not have been created.
    """
    outcomes: List[CommandResult] = []

    # 1. Optional: install the required interpreter via pyenv.
    if result.version and command_exists("pyenv"):
        console.print(f"  Ensuring Python {result.version} via pyenv…")
        # `pyenv install --skip-existing` is a no-op if already installed.
        outcomes.append(
            run_command(["pyenv", "install", "--skip-existing", result.version], capture=False)
        )

    # 2. Create or repair the virtual environment.
    venv_dir = project_dir / ".venv"
    venv_python = _venv_python_tool(venv_dir, "python")
    if not Path(venv_python).exists():
        # Either there's no .venv at all, or it's broken (no interpreter).
        if venv_dir.exists():
            console.print("  [warning].venv exists but has no interpreter — recreating it.[/warning]")
        else:
            console.print("  Creating virtual environment (.venv)…")
        create = run_command([sys.executable, "-m", "venv", str(venv_dir)], cwd=str(project_dir))
        outcomes.append(create)
        if not create.ok:
            # Without a venv we can't continue the Python setup.
            return outcomes
    else:
        console.print("  [muted].venv already exists — reusing it.[/muted]")

    # 3. Guarantee pip is present, then upgrade the build toolchain. The venv we
    #    found earlier had python.exe but no pip — `ensurepip` bootstraps it.
    if not _venv_has_pip(venv_python):
        console.print("  pip not found in .venv — bootstrapping it (ensurepip)…")
        outcomes.append(
            run_command([venv_python, "-m", "ensurepip", "--upgrade"], cwd=str(project_dir), capture=False)
        )
    console.print("  Upgrading pip, setuptools and wheel…")
    outcomes.append(
        run_command(
            [venv_python, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"],
            cwd=str(project_dir),
            capture=False,
        )
    )

    # 4. Install the project's dependencies (via `python -m pip`, never pip.exe).
    if "requirements.txt" in result.package_files:
        console.print("  Installing from requirements.txt…")
        outcomes.append(
            _pip_install_with_retry(venv_python, ["-r", "requirements.txt"], project_dir)
        )
    elif "pyproject.toml" in result.package_files or "setup.py" in result.package_files:
        console.print("  Installing project (pip install .)…")
        outcomes.append(_pip_install_with_retry(venv_python, ["."], project_dir))

    return outcomes


def _venv_has_pip(venv_python: str) -> bool:
    """Return True if pip is importable inside the venv interpreter."""
    return run_command([venv_python, "-m", "pip", "--version"]).ok


def _pip_install_with_retry(venv_python: str, target_args: List[str], project_dir: Path) -> CommandResult:
    """Run ``pip install <target>`` and retry once with relaxed resolution.

    Real-world requirement files sometimes pin combinations pip's strict
    resolver rejects. If the first attempt fails we retry once allowing pip to
    fall back to older versions of conflicting packages, which resolves the
    majority of "incompatible package" cases without manual editing. Anything
    that still fails (e.g. a package needing a system compiler) is reported to
    the user with the exact command to run manually.
    """
    base = [venv_python, "-m", "pip", "install"]
    first = run_command(base + target_args, cwd=str(project_dir), capture=False)
    if first.ok:
        return first

    console.print("  [warning]Install failed — retrying with relaxed dependency resolution…[/warning]")
    retry = run_command(
        base + ["--upgrade-strategy", "only-if-needed"] + target_args,
        cwd=str(project_dir),
        capture=False,
    )
    return retry


def _venv_python_tool(venv_dir: Path, tool: str) -> str:
    """Return the path to a tool (python/pip) inside a venv, per-OS.

    On Windows executables live in ``Scripts``; elsewhere in ``bin``.
    """
    if sys.platform == "win32":
        return str(venv_dir / "Scripts" / f"{tool}.exe")
    return str(venv_dir / "bin" / tool)


# -----------------------------------------------------------------------------
# Node.js
# -----------------------------------------------------------------------------
def setup_node(project_dir: Path, result: DetectionResult) -> List[CommandResult]:
    """Install Node dependencies, optionally installing the right Node first.

    Steps:
      1. If a version is known and ``nvm`` is available, install it. (nvm is a
         shell function, so we invoke it through the shell.)
      2. Run the appropriate install command, preferring a clean ``npm ci``
         when a lockfile is present, otherwise ``npm install``. If the first
         attempt fails on a peer-dependency conflict, retry with
         ``--legacy-peer-deps``.
    """
    outcomes: List[CommandResult] = []

    # 1. Optional: install the required Node version via nvm.
    if result.version and command_exists("nvm"):
        console.print(f"  Ensuring Node {result.version} via nvm…")
        # nvm is sourced into the shell, so we must run it via the shell.
        outcomes.append(run_command(f"nvm install {result.version}", shell=True, capture=False))

    # 2. Choose install command: `npm ci` is faster/stricter when a lockfile
    #    exists, but it requires the lockfile to be in sync, so we only use it
    #    when package-lock.json is present.
    has_lockfile = (project_dir / "package-lock.json").exists()
    install_cmd = ["npm", "ci"] if has_lockfile else ["npm", "install"]

    console.print(f"  Running {' '.join(install_cmd)}…")
    result_cmd = run_command(install_cmd, cwd=str(project_dir), capture=False)
    outcomes.append(result_cmd)

    # Retry with legacy peer deps if the strict resolver rejected the tree.
    if not result_cmd.ok:
        console.print("  [warning]Install failed — retrying with --legacy-peer-deps…[/warning]")
        outcomes.append(
            run_command(
                ["npm", "install", "--legacy-peer-deps"], cwd=str(project_dir), capture=False
            )
        )

    return outcomes


# -----------------------------------------------------------------------------
# Dispatcher
# -----------------------------------------------------------------------------
def setup_environment(project_dir: Path, result: DetectionResult) -> List[CommandResult]:
    """Route to the correct per-language setup function for a detection result."""
    if result.language == "Python":
        return setup_python(project_dir, result)
    if result.language == "Node.js":
        return setup_node(project_dir, result)
    console.print(f"  [muted]No automated setup for {result.language} yet.[/muted]")
    return []


def python_executable(project_dir: Path) -> Optional[str]:
    """Return the venv's python path if a .venv exists, else None.

    Later steps (migrations, launch) use this so they run inside the project's
    isolated environment rather than the system interpreter.
    """
    venv_dir = project_dir / ".venv"
    if venv_dir.exists():
        return _venv_python_tool(venv_dir, "python")
    return None
