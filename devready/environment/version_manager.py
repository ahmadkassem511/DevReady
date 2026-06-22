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
      2. Create ``.venv`` in the project directory (idempotent — skipped if it
         already exists).
      3. Install dependencies from requirements.txt or pyproject.toml using the
         venv's pip, which resolves conflicts with pip's built-in resolver.
    """
    outcomes: List[CommandResult] = []

    # 1. Optional: install the required interpreter via pyenv.
    if result.version and command_exists("pyenv"):
        console.print(f"  Ensuring Python {result.version} via pyenv…")
        # `pyenv install --skip-existing` is a no-op if already installed.
        outcomes.append(
            run_command(["pyenv", "install", "--skip-existing", result.version], capture=False)
        )

    # 2. Create the virtual environment if it doesn't exist yet.
    venv_dir = project_dir / ".venv"
    if not venv_dir.exists():
        console.print("  Creating virtual environment (.venv)…")
        # Use the interpreter currently running DevReady to bootstrap the venv.
        outcomes.append(
            run_command([sys.executable, "-m", "venv", str(venv_dir)], cwd=str(project_dir))
        )
    else:
        console.print("  [muted].venv already exists — reusing it.[/muted]")

    # 3. Install dependencies using the venv's own pip.
    pip = _venv_python_tool(venv_dir, "pip")
    if "requirements.txt" in result.package_files:
        console.print("  Installing from requirements.txt…")
        outcomes.append(
            run_command([pip, "install", "-r", "requirements.txt"], cwd=str(project_dir), capture=False)
        )
    elif "pyproject.toml" in result.package_files or "setup.py" in result.package_files:
        console.print("  Installing project (pip install .)…")
        outcomes.append(
            run_command([pip, "install", "."], cwd=str(project_dir), capture=False)
        )

    return outcomes


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
