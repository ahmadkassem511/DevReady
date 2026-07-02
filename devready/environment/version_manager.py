"""Set up language runtimes and install project dependencies.

This module handles Step 4 of ``devready start``: making sure the right runtime
version is available and installing the project's dependencies into an isolated
environment.

For Python, DevReady picks the *correct interpreter version per project* and
builds that project's own ``.venv`` from it — so two projects needing different
Python versions never interfere, and the system Python is never modified. The
interpreter is resolved with a "smart hybrid" strategy:

  1. Reuse an already-installed interpreter that matches (the running one, the
     Windows ``py`` launcher, ``python3.X`` on PATH, or one ``uv`` knows about).
  2. Only if none exists, download the exact version with ``uv`` into uv's own
     isolated cache (no admin rights, no system changes, no effect on other
     projects).

For Node it runs ``npm install`` and, when an ``.nvmrc``/engine version is known
and ``nvm`` is available, installs that Node version first.

Everything is best-effort and clearly reported: if a version can't be obtained
we proceed with the current runtime and warn, rather than hard-failing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from ..detectors import DetectionResult
from ..utils import CommandResult, command_exists, console, run_command


# -----------------------------------------------------------------------------
# Python
# -----------------------------------------------------------------------------
def setup_python(project_dir: Path, result: DetectionResult, healer=None) -> List[CommandResult]:
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

    # 1. Resolve the correct interpreter for THIS project (see module docstring).
    interpreter = resolve_python_interpreter(result.version)
    if interpreter is None:
        console.print(
            f"  [warning]Couldn't obtain Python {result.version}. Falling back to the current "
            f"interpreter — some packages may not install correctly.[/warning]"
        )
        interpreter = sys.executable
    target_v = _interpreter_version(interpreter)
    if result.version and target_v:
        console.print(f"  Using Python {target_v[0]}.{target_v[1]} for this project.")

    # 2. Create or repair the venv, making sure it matches the chosen version.
    venv_dir = project_dir / ".venv"
    venv_python = _venv_python_tool(venv_dir, "python")
    need_create = not Path(venv_python).exists()

    if not need_create:
        # A venv exists — but is it the right Python version? If a 3.14 venv is
        # sitting in a project that needs 3.11, reusing it would break installs.
        existing_v = _interpreter_version(venv_python)
        if target_v and existing_v and existing_v != target_v:
            console.print(
                f"  [warning].venv is Python {existing_v[0]}.{existing_v[1]} but this project "
                f"needs {target_v[0]}.{target_v[1]} — recreating it.[/warning]"
            )
            shutil.rmtree(venv_dir, ignore_errors=True)
            need_create = True
        else:
            console.print("  [muted].venv already exists and matches — reusing it.[/muted]")

    if need_create:
        if venv_dir.exists():
            console.print("  [warning].venv exists but is broken — recreating it.[/warning]")
            shutil.rmtree(venv_dir, ignore_errors=True)
        else:
            console.print("  Creating virtual environment (.venv)…")
        # Build the venv FROM the resolved interpreter, so the venv is that version.
        create = run_command([interpreter, "-m", "venv", str(venv_dir)], cwd=str(project_dir))
        outcomes.append(create)
        if not create.ok:
            # Without a venv we can't continue the Python setup.
            return outcomes

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
            _pip_install(venv_python, ["-r", "requirements.txt"], project_dir, healer)
        )
    elif "pyproject.toml" in result.package_files or "setup.py" in result.package_files:
        # EASIEST-PATH RESOLVER: when the project publishes an official prebuilt
        # package (its README documents `pip install <its-own-name>`) and building
        # from source would be heavy (it bundles a JS frontend that source builds
        # must compile), install the published wheel instead — minutes, not a
        # 15+-minute Node+ML build. Falls back to `pip install .` if that fails.
        published = _published_package_install(venv_python, project_dir, healer)
        if published is not None:
            outcomes.append(published)
        else:
            console.print("  Installing project (pip install .)…")
            outcomes.append(_pip_install(venv_python, ["."], project_dir, healer))

    return outcomes


def _project_package_name(project_dir: Path) -> Optional[str]:
    """The distribution name declared in pyproject.toml's [project] table, or None."""
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.exists():
        return None
    try:
        text = pyproject.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:  # proper parse when available (Python 3.11+)
        import tomllib
        name = tomllib.loads(text).get("project", {}).get("name")
        return str(name) if name else None
    except Exception:
        pass
    # Regex fallback: the first `name = "..."` after a [project] header.
    match = re.search(r"^\[project\]\s*$(.*?)^\[", text + "\n[", re.M | re.S)
    section = match.group(1) if match else ""
    name_match = re.search(r'^\s*name\s*=\s*["\']([^"\']+)["\']', section, re.M)
    return name_match.group(1) if name_match else None


def _published_package_install(
    venv_python: str, project_dir: Path, healer
) -> Optional[CommandResult]:
    """Install the project's own PUBLISHED package instead of building source.

    Only when all three hold (deliberately conservative):
      * pyproject.toml names the package;
      * the README documents installing that exact name from PyPI
        (``pip install <name>``) — i.e. the project officially ships prebuilt;
      * a source build would be heavy — the repo bundles a JS frontend
        (package.json at the root) that ``pip install .`` would have to compile.

    Returns the successful CommandResult, or None to fall back to source
    (including when the published install fails — source stays the safety net).
    """
    name = _project_package_name(project_dir)
    if not name:
        return None
    if not (project_dir / "package.json").exists():
        return None  # source build isn't the heavy JS+Python kind — just build it

    readme_text = ""
    for candidate in ("README.md", "README.rst", "README.txt", "README"):
        readme = project_dir / candidate
        if readme.exists():
            try:
                readme_text = readme.read_text(encoding="utf-8", errors="replace")
            except OSError:
                readme_text = ""
            break
    if not re.search(rf"pip3?\s+install\s+(-U\s+|--upgrade\s+)?{re.escape(name)}\b",
                     readme_text, re.IGNORECASE):
        return None

    console.print(
        f"  This project ships an official prebuilt package — installing "
        f"[bold]{name}[/bold] from PyPI instead of building from source "
        f"(much faster; frontend comes pre-built)…"
    )
    result = _pip_install(venv_python, [name], project_dir, healer)
    if result.ok:
        return result
    console.print(
        "  [warning]Published-package install didn't succeed — building from "
        "source instead.[/warning]"
    )
    return None


def _pip_install(
    venv_python: str, target_args: List[str], project_dir: Path, healer
) -> CommandResult:
    """Install Python deps, healing on failure when a healer is provided.

    With a healer, the install goes through the self-healing executor (built-in
    relaxed-resolution retry + LLM diagnosis). Without one, it uses the original
    offline relaxed-retry helper, so the no-LLM path is unchanged.
    """
    if healer is not None:
        cmd = [venv_python, "-m", "pip", "install"] + target_args
        return healer.run_step(cmd, cwd=str(project_dir), description="pip install")
    return _pip_install_with_retry(venv_python, target_args, project_dir)


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
# Python interpreter resolution (the "smart hybrid" version manager)
# -----------------------------------------------------------------------------
# The newest Python line with broad prebuilt-wheel coverage across the ecosystem.
# When a project pins no version and the interpreter running DevReady is newer
# than this, common packages (numpy, pandas, torch, cryptography, …) often have
# no wheels yet and fall back to slow/failing source builds — so we provision a
# well-supported line instead. Bump this as the ecosystem catches up.
_WELL_SUPPORTED_PYTHON = (3, 12)
_PREFERRED_PYTHON_FALLBACKS = ("3.12", "3.11")


def _current_python_version() -> Tuple[int, int]:
    """Return the (major, minor) of the interpreter running DevReady."""
    return (sys.version_info.major, sys.version_info.minor)


def resolve_python_interpreter(required_version: Optional[str]) -> Optional[str]:
    """Return a path to a Python interpreter for this project.

    Strategy:
      1. If a version is required, reuse an installed match or download it (uv).
      2. If none is required, use the running interpreter — UNLESS it's newer
         than the well-supported line, in which case prefer a broadly-compatible
         version (3.12/3.11) so packages install from wheels rather than failing
         to build from source on a bleeding-edge interpreter.
    """
    if required_version:
        found = find_installed_python(required_version)
        if found:
            return found
        console.print(f"  No Python {required_version} found locally — fetching it with uv…")
        return install_python_with_uv(required_version)

    # No version pinned.
    current = _current_python_version()
    if current <= _WELL_SUPPORTED_PYTHON:
        return sys.executable  # already a well-supported version — use it

    # The running interpreter is newer than the well-supported line. Prefer a
    # stable one for the best package compatibility (reuse if present, else fetch).
    for ver in _PREFERRED_PYTHON_FALLBACKS:
        found = find_installed_python(ver)
        if found:
            console.print(
                f"  Using Python {ver} for the best package compatibility "
                f"(your default Python is {current[0]}.{current[1]}, which many packages "
                f"don't ship wheels for yet)."
            )
            return found
    for ver in _PREFERRED_PYTHON_FALLBACKS:
        console.print(f"  Fetching Python {ver} with uv for broad package compatibility…")
        installed = install_python_with_uv(ver)
        if installed:
            return installed

    # Couldn't obtain a stable line — proceed with the current interpreter.
    return sys.executable


def find_installed_python(required_version: str) -> Optional[str]:
    """Find an already-installed interpreter matching ``required_version``.

    Checks, in order: the running interpreter, the Windows ``py`` launcher,
    ``python3.X`` on PATH, and any interpreter ``uv`` already knows about
    (which includes system installs). Returns a path, or None if nothing fits.
    Never installs anything.
    """
    want = _parse_version(required_version)
    if want is None:
        return None
    major_minor = f"{want[0]}.{want[1]}"

    # 1. Is the interpreter running DevReady already the right version?
    if (sys.version_info.major, sys.version_info.minor) == want:
        return sys.executable

    # 2. Windows 'py' launcher can locate a specific version precisely.
    if sys.platform == "win32":
        res = run_command(["py", f"-{major_minor}", "-c", "import sys; print(sys.executable)"])
        path = res.stdout.strip().splitlines()[-1].strip() if res.ok and res.stdout.strip() else ""
        if path and Path(path).exists():
            return path

    # 3. A conventionally-named interpreter on PATH (python3.11, etc.).
    for name in (f"python{major_minor}", f"python{major_minor}.exe"):
        path = shutil.which(name)
        if path:
            return path

    # 4. Ask uv — it discovers managed *and* system interpreters.
    uv = _uv_executable()
    if uv:
        res = run_command([uv, "python", "find", major_minor])
        path = res.stdout.strip().splitlines()[-1].strip() if res.stdout.strip() else ""
        # `uv python find` prints the error to stderr and leaves stdout empty
        # when nothing matches, so we validate the path exists.
        if path and Path(path).exists():
            return path

    return None


def install_python_with_uv(required_version: str) -> Optional[str]:
    """Download ``required_version`` via uv into uv's isolated cache.

    This does not touch system Python or other projects — uv keeps managed
    interpreters in its own directory. Returns the new interpreter's path, or
    None if uv is unavailable or the download failed.
    """
    uv = _ensure_uv()
    if not uv:
        console.print("  [warning]uv is not available, so the version can't be auto-installed.[/warning]")
        return None

    want = _parse_version(required_version)
    major_minor = f"{want[0]}.{want[1]}" if want else required_version

    install = run_command([uv, "python", "install", major_minor], capture=False)
    if not install.ok:
        return None

    res = run_command([uv, "python", "find", major_minor])
    path = res.stdout.strip().splitlines()[-1].strip() if res.stdout.strip() else ""
    return path if path and Path(path).exists() else None


def _uv_executable() -> Optional[str]:
    """Locate a usable uv executable, or None.

    Prefers uv on PATH; otherwise looks next to the interpreter running DevReady
    (where ``pip install uv`` would have placed it).
    """
    if command_exists("uv"):
        return "uv"
    scripts = Path(sys.executable).parent / ("Scripts" if sys.platform == "win32" else "bin")
    candidate = scripts / ("uv.exe" if sys.platform == "win32" else "uv")
    return str(candidate) if candidate.exists() else None


def _ensure_uv() -> Optional[str]:
    """Return a uv executable, installing uv via pip if it isn't present yet."""
    existing = _uv_executable()
    if existing:
        return existing
    console.print("  Installing uv (one-time, manages Python versions)…")
    run_command([sys.executable, "-m", "pip", "install", "uv"], capture=False)
    return _uv_executable()


def _parse_version(version: str) -> Optional[Tuple[int, int]]:
    """Parse a version string like '3.11' or '3.11.4' into a (major, minor) tuple."""
    match = re.match(r"(\d+)\.(\d+)", version.strip())
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def _interpreter_version(python_path: str) -> Optional[Tuple[int, int]]:
    """Return the (major, minor) version of an interpreter, or None if it fails."""
    res = run_command([python_path, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"])
    if res.ok:
        return _parse_version(res.stdout.strip())
    return None


# -----------------------------------------------------------------------------
# Node.js
# -----------------------------------------------------------------------------
def setup_node(project_dir: Path, result: DetectionResult, healer=None) -> List[CommandResult]:
    """Install Node dependencies, ensuring the right Node version when possible.

    Mirrors the Python flow's philosophy of per-project versions:
      1. If the project needs a specific Node version, try to honour it without
         touching the global default — via ``fnm`` (modern, runs through
         ``fnm exec`` so no shell integration is required) or ``nvm`` if present.
         If neither is installed we warn and proceed with the current Node.
      2. Install dependencies: ``npm ci`` when a lockfile is present (faster,
         reproducible), otherwise ``npm install``; retry once with
         ``--legacy-peer-deps`` if the strict peer resolver rejects the tree.
    """
    outcomes: List[CommandResult] = []

    # Node may not be installed at all. Auto-install it (it bundles npm) so the
    # project doesn't dead-end — the same philosophy as uv for Python. If we
    # can't get npm onto PATH, stop here with a clear message instead of running
    # `npm` and reporting a cryptic "command not found".
    if not command_exists("npm"):
        from . import system_deps

        if not system_deps.ensure_node():
            return outcomes

    # When the project pins a Node version the current one doesn't meet, we make
    # the right Node available for THIS project only — the user's default Node is
    # untouched. Rather than prefixing every command with `fnm exec` (which on
    # Windows can't spawn the .cmd shims npm/corepack/pnpm are), we put the pinned
    # Node's own bin dir on PATH and spawn the package manager ourselves — so
    # DevReady's .cmd resolution works. fnm is auto-installed if needed.
    node_env: Optional[dict] = None  # custom environment with the pinned Node first
    npm_prefix: List[str] = []        # fallback prefix when we can't get the bin dir

    if result.version and not _node_satisfies(result.version):
        if not command_exists("fnm") and not command_exists("nvm"):
            from . import system_deps

            console.print(
                f"  This project needs Node {result.version} (you have {_node_version() or 'none'}); "
                f"installing fnm to manage Node versions…"
            )
            system_deps.install_tool("fnm")
        if command_exists("fnm"):
            console.print(f"  Ensuring Node {result.version} via fnm (isolated to this project)…")
            inst = run_command(["fnm", "install", result.version], capture=False)
            outcomes.append(inst)
            if inst.ok:
                bin_dir = _fnm_node_bin_dir(result.version)
                if bin_dir:
                    node_env = os.environ.copy()
                    node_env["PATH"] = bin_dir + os.pathsep + node_env.get("PATH", "")
                    console.print(f"  Using Node {result.version} from fnm for this project.")
                else:
                    # Couldn't resolve the bin dir — fall back to the exec prefix.
                    npm_prefix = ["fnm", "exec", "--using", result.version, "--"]
            else:
                console.print(
                    f"  [warning]Couldn't install Node {result.version} via fnm — "
                    f"proceeding with the current Node.[/warning]"
                )
        elif command_exists("nvm"):
            console.print(f"  Ensuring Node {result.version} via nvm…")
            outcomes.append(run_command(f"nvm install {result.version}", shell=True, capture=False))
        else:
            console.print(
                f"  [warning]This project targets Node {result.version} but the installed Node is "
                f"{_node_version()}; proceeding with the current Node.[/warning]"
            )

    # If this project's npm scripts are Unix shell scripts, run them through bash
    # (Git Bash) instead of cmd.exe — so a `postinstall: build/foo.sh` works
    # during install AND `npm run dev` works at launch, on Windows. Carried via
    # the env so no project files are modified.
    bash_shell = needs_bash_script_shell(project_dir)
    if bash_shell:
        if node_env is None:
            node_env = os.environ.copy()
        node_env["npm_config_script_shell"] = bash_shell
        console.print("  This project's scripts are shell scripts — using bash to run them.")

    # 2. Install dependencies using the package manager the project actually
    #    uses — detected from its lockfile. A yarn/pnpm project installed with
    #    npm often breaks, so honour yarn.lock / pnpm-lock.yaml.
    pm = _node_package_manager(project_dir)

    # Run yarn/pnpm THROUGH corepack (bundled with Node 16.10+) whenever it's
    # available. Corepack reads the project's pinned version (package.json
    # "packageManager", or engines) and provisions exactly that — so a project
    # needing pnpm 10 isn't run with a stale global pnpm 9. It also needs no
    # global shims or admin rights. We disable the interactive download prompt
    # so an unattended run never hangs. corepack is looked up on the *effective*
    # PATH (the pinned Node's, when one is active) — not the system default.
    search_path = node_env.get("PATH") if node_env else None
    if node_env is not None:
        node_env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"
    else:
        os.environ.setdefault("COREPACK_ENABLE_DOWNLOAD_PROMPT", "0")

    pm_runner: List[str] = [pm]
    if pm != "npm":
        if _which_on_path("corepack", search_path):
            pm_runner = ["corepack", pm]
            # Enable corepack so a *bare* `pnpm`/`yarn` (as a project's own `npm run
            # dev` script invokes) resolves to the project's pinned version too —
            # not a stale global one. This is what makes the later launch work, not
            # just this install. Best-effort; harmless if it no-ops.
            if node_env is not None:
                run_command(["corepack", "enable"], env=node_env)
        elif not _which_on_path(pm, search_path):
            console.print(f"  [warning]{pm} isn't available — using npm instead.[/warning]")
            pm = "npm"

    if pm in ("yarn", "pnpm"):
        install_cmd = npm_prefix + pm_runner + ["install"]
    else:
        has_lockfile = (project_dir / "package-lock.json").exists()
        install_cmd = npm_prefix + (["npm", "ci"] if has_lockfile else ["npm", "install"])

    console.print(f"  Running {' '.join(install_cmd)}…")
    if healer is not None:
        # The healer streams + captures, does the --legacy-peer-deps retry, and
        # (with an LLM key) diagnoses anything else that fails.
        outcomes.append(
            healer.run_step(
                install_cmd, cwd=str(project_dir), description="npm install", env=node_env
            )
        )
        return outcomes

    result_cmd = run_command(install_cmd, cwd=str(project_dir), capture=False, env=node_env)
    outcomes.append(result_cmd)

    # A peer-dependency conflict is common and fixable; retry once with
    # --legacy-peer-deps (npm only). (Exit 127 means the tool itself wasn't
    # found — a different problem handled above — so retrying wouldn't help.)
    if pm == "npm" and not result_cmd.ok and result_cmd.returncode != 127:
        console.print("  [warning]Install failed — retrying with --legacy-peer-deps…[/warning]")
        outcomes.append(
            run_command(
                npm_prefix + ["npm", "install", "--legacy-peer-deps"],
                cwd=str(project_dir),
                capture=False,
                env=node_env,
            )
        )

    return outcomes


def _node_package_manager(project_dir: Path) -> str:
    """Return the Node package manager the project uses, based on its lockfile."""
    if (project_dir / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (project_dir / "yarn.lock").exists():
        return "yarn"
    return "npm"


def _which_on_path(name: str, path: Optional[str]) -> bool:
    """True if ``name`` is found on ``path`` (or the default PATH when None).

    Unlike ``command_exists``, this can search a specific PATH — used to check
    whether corepack/pnpm exist inside a pinned Node's bin dir rather than the
    system default.
    """
    return shutil.which(name, path=path) is not None


def _git_bash() -> Optional[str]:
    """Locate a *real* Git Bash, never ``C:\\Windows\\System32\\bash.exe``.

    System32's ``bash.exe`` is the WSL launcher; on a machine with no WSL distro
    it fails with ``WSL_E_DEFAULT_DISTRO_NOT_FOUND``. Since System32 is usually
    early on PATH, a bare ``which("bash")`` finds that stub first — so we skip it
    and resolve Git Bash from git's own location / the standard install dirs.
    """
    candidate = shutil.which("bash")
    if candidate and "system32" not in candidate.lower():
        return candidate

    roots: List[Path] = []
    git = shutil.which("git")
    if git:
        # …/Git/cmd/git.exe or …/Git/mingw64/bin/git.exe → walk up to …/Git
        p = Path(git).resolve()
        roots += [p.parent.parent, p.parent.parent.parent]
    roots += [
        Path(r"C:\Program Files\Git"),
        Path(r"C:\Program Files (x86)\Git"),
        Path(os.path.expanduser(r"~\AppData\Local\Programs\Git")),
        Path(os.path.expanduser(r"~\scoop\apps\git\current")),
    ]
    for root in roots:
        for rel in ("bin/bash.exe", "usr/bin/bash.exe"):
            bash = root / rel
            if bash.exists():
                return str(bash)
    return None


def needs_bash_script_shell(project_dir: Path) -> Optional[str]:
    """Return a bash path to use as npm's ``script-shell``, or None.

    On Windows, npm runs ``package.json`` scripts through ``cmd.exe`` by default.
    Repos written for Unix put shell scripts in their lifecycle/run scripts
    (e.g. ``"postinstall": "build/foo.sh"``), which cmd can't execute —
    ``'build' is not recognized…``. When such a project is detected *and* Git
    Bash is available, pointing npm's ``script-shell`` at it lets those scripts
    run for real, for both ``npm install``'s lifecycle scripts and ``npm run`` at
    launch — rather than skipping them.

    Returns None when not needed: non-Windows (sh already handles ``.sh``), no
    Git Bash available, or the project has no shell-script npm scripts.
    """
    if sys.platform != "win32":
        return None
    bash = _git_bash()
    if not bash:
        return None
    pkg = project_dir / "package.json"
    if not pkg.exists():
        return None
    try:
        scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
    except (json.JSONDecodeError, OSError):
        return None
    # A script that invokes a `.sh` file is a strong, low-false-positive signal
    # that the repo expects a Unix shell.
    blob = " ".join(str(v) for v in (scripts or {}).values()).lower()
    return bash if ".sh" in blob else None


def _fnm_node_bin_dir(version: str) -> Optional[str]:
    """Return the bin directory of the fnm-managed Node for ``version``.

    We ask fnm to run ``node`` (which it *can* spawn — it's ``node.exe``) and
    print its own executable directory. Putting that dir on PATH lets DevReady
    invoke npm/corepack/pnpm itself, sidestepping fnm's inability to spawn the
    ``.cmd`` shims those tools are on Windows. Returns None if it can't be found.
    """
    res = run_command(
        [
            "fnm", "exec", "--using", version, "--",
            "node", "-e", "process.stdout.write(require('path').dirname(process.execPath))",
        ]
    )
    if not res.ok or not res.stdout.strip():
        return None
    path = res.stdout.strip().splitlines()[-1].strip()
    return path if path and Path(path).exists() else None


def _node_version() -> Optional[str]:
    """Return the installed Node version string (e.g. '20.10.0'), or None."""
    res = run_command(["node", "--version"])
    if res.ok:
        return res.stdout.strip().lstrip("v")
    return None


def _node_matches(required: str) -> bool:
    """True if the installed Node's major version matches the required one."""
    installed = _node_version()
    if not installed:
        return False
    req_major = re.match(r"(\d+)", required.strip())
    inst_major = re.match(r"(\d+)", installed)
    return bool(req_major and inst_major and req_major.group(1) == inst_major.group(1))


def _version_tuple(v: str) -> Optional[Tuple[int, int]]:
    """Parse 'major' or 'major.minor' (ignoring any suffix) into a (major, minor) tuple."""
    m = re.match(r"(\d+)(?:\.(\d+))?", (v or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0)


def _node_satisfies(required: str) -> bool:
    """True if the installed Node meets the required (major, minor) or newer.

    Used to decide whether to manage a per-project Node version: a project that
    pins ``>=22.22`` isn't satisfied by 22.21, so DevReady provisions the right
    version via fnm. A pin of just ``18`` is met by any 18.x or newer.
    """
    installed = _node_version()
    if not installed:
        return False
    req, inst = _version_tuple(required), _version_tuple(installed)
    if not req or not inst:
        return False
    return inst >= req


# -----------------------------------------------------------------------------
# Compiled / other-ecosystem languages
# -----------------------------------------------------------------------------
def _toolchain_setup(
    project_dir: Path,
    *,
    language: str,
    runner: str,
    install_cmd: List[str],
    install_hint: str,
    healer=None,
) -> List[CommandResult]:
    """Shared "install deps with a single command-line tool" setup.

    Used by the Rust/Go/Ruby/PHP setups, which (unlike Python) don't need a
    virtualenv — their package managers install into a project-local location
    on their own. If the required tool isn't installed, DevReady installs it
    (via the system package manager) and continues — the same philosophy as uv
    for Python and corepack for Node — rather than dead-ending on a missing tool.
    """
    if not command_exists(runner):
        from . import system_deps

        console.print(f"  {language} needs '{runner}', which isn't installed — installing it…")
        if not system_deps.install_tool(runner):
            console.print(
                f"  [warning]Couldn't install '{runner}' automatically. "
                f"Install it ({install_hint}) and re-run.[/warning]"
            )
            return []
    console.print(f"  Installing {language} dependencies ({' '.join(install_cmd)})…")
    return [_run_install(install_cmd, project_dir, healer, f"{language} dependencies")]


def setup_rust(project_dir: Path, result: DetectionResult, healer=None) -> List[CommandResult]:
    """Fetch and build a Rust project's dependencies with cargo."""
    return _toolchain_setup(
        project_dir,
        language="Rust",
        runner="cargo",
        install_cmd=["cargo", "build"],
        install_hint="https://rustup.rs",
        healer=healer,
    )


def setup_go(project_dir: Path, result: DetectionResult, healer=None) -> List[CommandResult]:
    """Download a Go module's dependencies."""
    return _toolchain_setup(
        project_dir,
        language="Go",
        runner="go",
        install_cmd=["go", "mod", "download"],
        install_hint="https://go.dev/dl/",
        healer=healer,
    )


def setup_ruby(project_dir: Path, result: DetectionResult, healer=None) -> List[CommandResult]:
    """Install a Ruby project's gems with Bundler.

    If Bundler itself is missing but RubyGems is present, install it first.
    """
    outcomes: List[CommandResult] = []
    if not command_exists("bundle"):
        if not command_exists("gem"):
            # No Ruby at all — install it (it ships gem), then continue.
            from . import system_deps

            console.print("  Ruby isn't installed — installing it…")
            if not system_deps.install_tool("ruby") or not command_exists("gem"):
                console.print(
                    "  [warning]Couldn't install Ruby automatically. "
                    "Install it (https://www.ruby-lang.org) and re-run.[/warning]"
                )
                return outcomes
        console.print("  Bundler not found — installing it (gem install bundler)…")
        outcomes.append(run_command(["gem", "install", "bundler"], cwd=str(project_dir), capture=False))
    console.print("  Installing Ruby dependencies (bundle install)…")
    outcomes.append(_run_install(["bundle", "install"], project_dir, healer, "Ruby dependencies"))
    return outcomes


# Extensions composer and most PHP apps need. A freshly-installed PHP (esp. via
# scoop/winget) often ships the DLLs but with no active php.ini, so they're off —
# which makes `composer install` die with "the openssl extension is required".
_PHP_COMMON_EXTENSIONS = [
    "openssl", "curl", "mbstring", "fileinfo", "zip", "gd", "intl",
    "pdo_mysql", "pdo_sqlite", "sodium", "bcmath", "exif", "gmp",
]


def ensure_php_extensions() -> None:
    """Make sure PHP has an active php.ini with the common extensions enabled.

    Locates the real PHP via ``PHP_BINARY``; creates ``php.ini`` from the bundled
    production template if none is loaded; sets ``extension_dir`` and enables each
    common extension whose DLL is actually shipped. Idempotent and best-effort —
    never raises. This is what lets ``composer install`` (which needs openssl)
    and most PHP apps work on a fresh Windows PHP.
    """
    if not command_exists("php"):
        return
    res = run_command(["php", "-r", "echo PHP_BINARY;"])
    php_exe = res.stdout.strip().splitlines()[-1].strip() if res.ok and res.stdout.strip() else ""
    if not php_exe or not Path(php_exe).exists():
        return
    php_dir = Path(php_exe).parent
    ini = php_dir / "php.ini"

    if not ini.exists():
        template = next(
            (php_dir / name for name in ("php.ini-production", "php.ini-development")
             if (php_dir / name).exists()),
            None,
        )
        try:
            ini.write_text(
                template.read_text(encoding="utf-8", errors="replace") if template else "",
                encoding="utf-8",
            )
        except OSError:
            return

    try:
        text = ini.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    original = text

    ext_dir = php_dir / "ext"
    if ext_dir.exists():
        if re.search(r'^\s*;\s*extension_dir\s*=\s*"ext"', text, re.M):
            text = re.sub(r'^\s*;\s*extension_dir\s*=\s*"ext"', 'extension_dir = "ext"', text, flags=re.M)
        elif not re.search(r'^\s*extension_dir\s*=', text, re.M):
            text += f'\nextension_dir = "{ext_dir}"\n'

    enabled = []
    for ext in _PHP_COMMON_EXTENSIONS:
        # Only enable extensions whose DLL is actually present (Windows: php_<ext>.dll).
        if ext_dir.exists() and not (ext_dir / f"php_{ext}.dll").exists():
            continue
        commented = re.compile(rf'^\s*;\s*extension\s*=\s*{re.escape(ext)}\s*$', re.M)
        if commented.search(text):
            text = commented.sub(f"extension={ext}", text)
            enabled.append(ext)
        elif not re.search(rf'^\s*extension\s*=\s*{re.escape(ext)}\s*$', text, re.M):
            text += f"\nextension={ext}\n"
            enabled.append(ext)

    if text != original:
        try:
            ini.write_text(text, encoding="utf-8")
            console.print(f"  Enabled PHP extensions ({', '.join(enabled)}) so composer/PHP work.")
        except OSError:
            pass


def setup_php(project_dir: Path, result: DetectionResult, healer=None) -> List[CommandResult]:
    """Install a PHP project's dependencies with Composer.

    Composer is a PHP application (a ``.phar``) — it can't run without the PHP
    runtime, and it needs the openssl extension to fetch packages over HTTPS. So
    we ensure ``php`` is installed *and* its common extensions are enabled before
    running composer, otherwise ``composer install`` dies with "php is not
    recognized" or "the openssl extension is required".
    """
    from . import system_deps

    if not command_exists("php"):
        console.print("  PHP project, but the PHP runtime isn't installed — installing it…")
        if not system_deps.install_tool("php"):
            console.print(
                "  [warning]Couldn't install PHP automatically. "
                "Install it (https://www.php.net/downloads) and re-run.[/warning]"
            )
            return []

    ensure_php_extensions()

    return _toolchain_setup(
        project_dir,
        language="PHP",
        runner="composer",
        install_cmd=["composer", "install"],
        install_hint="https://getcomposer.org",
        healer=healer,
    )


def maven_executable(project_dir: Path) -> str:
    """Return the Maven wrapper in the project if present, else system ``mvn``.

    A wrapper (``mvnw``) pins the exact Maven version the project expects, so we
    prefer it. It lives in the project dir, so we return an absolute path —
    subprocess won't find a cwd-local script by bare name on Windows.
    """
    wrapper = project_dir / ("mvnw.cmd" if sys.platform == "win32" else "mvnw")
    return str(wrapper) if wrapper.exists() else "mvn"


def gradle_executable(project_dir: Path) -> str:
    """Return the Gradle wrapper in the project if present, else system ``gradle``."""
    wrapper = project_dir / ("gradlew.bat" if sys.platform == "win32" else "gradlew")
    return str(wrapper) if wrapper.exists() else "gradle"


def _runner_available(executable: str) -> bool:
    """True if an executable is runnable, whether it's a PATH tool or a wrapper path."""
    return command_exists(executable) or Path(executable).exists()


def setup_java(project_dir: Path, result: DetectionResult, healer=None) -> List[CommandResult]:
    """Build a Java project's dependencies with Maven or Gradle (wrapper-aware)."""
    from . import system_deps

    if (project_dir / "pom.xml").exists():
        exe = maven_executable(project_dir)
        if not _runner_available(exe):
            console.print("  Java/Maven project, but Maven isn't installed — installing it…")
            if not system_deps.install_tool("mvn"):
                console.print(
                    "  [warning]Couldn't install Maven automatically. "
                    "Install it (https://maven.apache.org) and re-run.[/warning]"
                )
                return []
            exe = maven_executable(project_dir)
        console.print("  Building with Maven (install -DskipTests)…")
        return [_run_install([exe, "install", "-DskipTests"], project_dir, healer, "Maven build")]

    # Otherwise it's a Gradle project.
    exe = gradle_executable(project_dir)
    if not _runner_available(exe):
        console.print("  Java/Gradle project, but Gradle isn't installed — installing it…")
        if not system_deps.install_tool("gradle"):
            console.print(
                "  [warning]Couldn't install Gradle automatically. "
                "Install it (https://gradle.org) and re-run.[/warning]"
            )
            return []
        exe = gradle_executable(project_dir)
    console.print("  Building with Gradle (build -x test)…")
    return [_run_install([exe, "build", "-x", "test"], project_dir, healer, "Gradle build")]


def setup_dotnet(project_dir: Path, result: DetectionResult, healer=None) -> List[CommandResult]:
    """Restore a .NET project's dependencies with the dotnet CLI."""
    return _toolchain_setup(
        project_dir,
        language=".NET",
        runner="dotnet",
        install_cmd=["dotnet", "restore"],
        install_hint="https://dotnet.microsoft.com/download",
        healer=healer,
    )


# -----------------------------------------------------------------------------
# Dispatcher
# -----------------------------------------------------------------------------
def setup_environment(project_dir: Path, result: DetectionResult, healer=None) -> List[CommandResult]:
    """Route to the correct per-language setup function for a detection result.

    ``healer`` is an optional :class:`devready.ai.healer.InstallHealer`. When
    given, dependency-install commands run through it so a failure is retried and
    (with an LLM key) auto-diagnosed and fixed instead of dead-ending.
    """
    setups = {
        "Python": setup_python,
        "Node.js": setup_node,
        "Rust": setup_rust,
        "Go": setup_go,
        "Ruby": setup_ruby,
        "PHP": setup_php,
        "Java": setup_java,
        ".NET": setup_dotnet,
    }
    setup_fn = setups.get(result.language)
    if setup_fn:
        return setup_fn(project_dir, result, healer)
    console.print(f"  [muted]No automated setup for {result.language} yet.[/muted]")
    return []


def _run_install(
    command: List[str], project_dir: Path, healer, description: str
) -> CommandResult:
    """Run a dependency-install command, healing on failure when a healer is set.

    Falls back to the plain streamed runner when no healer is provided, so the
    offline path (and existing tests that monkeypatch ``run_command``) is
    unchanged.
    """
    if healer is not None:
        return healer.run_step(command, cwd=str(project_dir), description=description)
    return run_command(command, cwd=str(project_dir), capture=False)


def python_executable(project_dir: Path) -> Optional[str]:
    """Return the venv's python path if a .venv exists, else None.

    Later steps (migrations, launch) use this so they run inside the project's
    isolated environment rather than the system interpreter.
    """
    venv_dir = project_dir / ".venv"
    if venv_dir.exists():
        return _venv_python_tool(venv_dir, "python")
    return None
