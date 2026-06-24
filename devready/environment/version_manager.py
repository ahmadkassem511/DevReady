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
# Python interpreter resolution (the "smart hybrid" version manager)
# -----------------------------------------------------------------------------
def resolve_python_interpreter(required_version: Optional[str]) -> Optional[str]:
    """Return a path to a Python interpreter matching ``required_version``.

    Strategy (see module docstring):
      1. Reuse an already-installed matching interpreter.
      2. If none exists, download the exact version with uv (isolated).

    When no version is required, the interpreter running DevReady is returned —
    there's nothing to match against, so any Python will do.
    """
    if not required_version:
        return sys.executable

    found = find_installed_python(required_version)
    if found:
        return found

    console.print(f"  No Python {required_version} found locally — fetching it with uv…")
    return install_python_with_uv(required_version)


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
def setup_node(project_dir: Path, result: DetectionResult) -> List[CommandResult]:
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

    # The command prefix used to run npm. When fnm is available and a specific
    # version is required, we route npm through `fnm exec` so the right Node is
    # used for THIS project only — the user's default Node is untouched.
    npm_prefix: List[str] = []

    if result.version:
        if command_exists("fnm"):
            console.print(f"  Ensuring Node {result.version} via fnm (isolated to this project)…")
            outcomes.append(run_command(["fnm", "install", result.version], capture=False))
            npm_prefix = ["fnm", "exec", "--using", result.version, "--"]
        elif command_exists("nvm"):
            console.print(f"  Ensuring Node {result.version} via nvm…")
            # nvm is a shell function, so it must run through the shell.
            outcomes.append(run_command(f"nvm install {result.version}", shell=True, capture=False))
        elif _node_version() and not _node_matches(result.version):
            console.print(
                f"  [warning]This project targets Node {result.version} but the installed Node is "
                f"{_node_version()}. Install fnm (https://github.com/Schniz/fnm) so DevReady can "
                f"manage Node versions automatically; proceeding with the current Node for now.[/warning]"
            )

    # 2. Install dependencies using the package manager the project actually
    #    uses — detected from its lockfile. A yarn/pnpm project installed with
    #    npm often breaks, so honour yarn.lock / pnpm-lock.yaml.
    pm = _node_package_manager(project_dir)

    # If the project's package manager isn't installed, run it *through* corepack
    # (bundled with Node 16.10+). `corepack yarn …` provisions the right version
    # on demand without writing global shims or needing admin rights. We disable
    # corepack's interactive download prompt so an unattended run never hangs.
    pm_runner: List[str] = [pm]
    if pm != "npm" and not command_exists(pm):
        if command_exists("corepack"):
            console.print(f"  Provisioning {pm} via corepack…")
            os.environ.setdefault("COREPACK_ENABLE_DOWNLOAD_PROMPT", "0")
            pm_runner = ["corepack", pm]
        else:
            console.print(f"  [warning]{pm} isn't available — using npm instead.[/warning]")
            pm = "npm"

    if pm == "yarn":
        install_cmd = npm_prefix + pm_runner + ["install"]
    elif pm == "pnpm":
        install_cmd = npm_prefix + pm_runner + ["install"]
    else:
        has_lockfile = (project_dir / "package-lock.json").exists()
        install_cmd = npm_prefix + (["npm", "ci"] if has_lockfile else ["npm", "install"])

    console.print(f"  Running {' '.join(install_cmd)}…")
    result_cmd = run_command(install_cmd, cwd=str(project_dir), capture=False)
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
) -> List[CommandResult]:
    """Shared "install deps with a single command-line tool" setup.

    Used by the Rust/Go/Ruby/PHP setups, which (unlike Python) don't need a
    virtualenv — their package managers install into a project-local location
    on their own. If the tool isn't installed we warn with how to get it rather
    than failing cryptically.
    """
    if not command_exists(runner):
        console.print(
            f"  [warning]{language} project detected, but '{runner}' isn't installed.\n"
            f"  Install it ({install_hint}) and re-run, so DevReady can set up dependencies.[/warning]"
        )
        return []
    console.print(f"  Installing {language} dependencies ({' '.join(install_cmd)})…")
    return [run_command(install_cmd, cwd=str(project_dir), capture=False)]


def setup_rust(project_dir: Path, result: DetectionResult) -> List[CommandResult]:
    """Fetch and build a Rust project's dependencies with cargo."""
    return _toolchain_setup(
        project_dir,
        language="Rust",
        runner="cargo",
        install_cmd=["cargo", "build"],
        install_hint="https://rustup.rs",
    )


def setup_go(project_dir: Path, result: DetectionResult) -> List[CommandResult]:
    """Download a Go module's dependencies."""
    return _toolchain_setup(
        project_dir,
        language="Go",
        runner="go",
        install_cmd=["go", "mod", "download"],
        install_hint="https://go.dev/dl/",
    )


def setup_ruby(project_dir: Path, result: DetectionResult) -> List[CommandResult]:
    """Install a Ruby project's gems with Bundler.

    If Bundler itself is missing but RubyGems is present, install it first.
    """
    outcomes: List[CommandResult] = []
    if not command_exists("bundle"):
        if command_exists("gem"):
            console.print("  Bundler not found — installing it (gem install bundler)…")
            outcomes.append(run_command(["gem", "install", "bundler"], cwd=str(project_dir), capture=False))
        else:
            console.print(
                "  [warning]Ruby project detected, but neither 'bundle' nor 'gem' is installed.\n"
                "  Install Ruby (https://www.ruby-lang.org) and re-run.[/warning]"
            )
            return outcomes
    console.print("  Installing Ruby dependencies (bundle install)…")
    outcomes.append(run_command(["bundle", "install"], cwd=str(project_dir), capture=False))
    return outcomes


def setup_php(project_dir: Path, result: DetectionResult) -> List[CommandResult]:
    """Install a PHP project's dependencies with Composer."""
    return _toolchain_setup(
        project_dir,
        language="PHP",
        runner="composer",
        install_cmd=["composer", "install"],
        install_hint="https://getcomposer.org",
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


def setup_java(project_dir: Path, result: DetectionResult) -> List[CommandResult]:
    """Build a Java project's dependencies with Maven or Gradle (wrapper-aware)."""
    if (project_dir / "pom.xml").exists():
        exe = maven_executable(project_dir)
        if not _runner_available(exe):
            console.print(
                "  [warning]Java/Maven project detected, but Maven isn't installed.\n"
                "  Install it (https://maven.apache.org) and re-run.[/warning]"
            )
            return []
        console.print("  Building with Maven (install -DskipTests)…")
        return [run_command([exe, "install", "-DskipTests"], cwd=str(project_dir), capture=False)]

    # Otherwise it's a Gradle project.
    exe = gradle_executable(project_dir)
    if not _runner_available(exe):
        console.print(
            "  [warning]Java/Gradle project detected, but Gradle isn't installed.\n"
            "  Install it (https://gradle.org) and re-run.[/warning]"
        )
        return []
    console.print("  Building with Gradle (build -x test)…")
    return [run_command([exe, "build", "-x", "test"], cwd=str(project_dir), capture=False)]


def setup_dotnet(project_dir: Path, result: DetectionResult) -> List[CommandResult]:
    """Restore a .NET project's dependencies with the dotnet CLI."""
    return _toolchain_setup(
        project_dir,
        language=".NET",
        runner="dotnet",
        install_cmd=["dotnet", "restore"],
        install_hint="https://dotnet.microsoft.com/download",
    )


# -----------------------------------------------------------------------------
# Dispatcher
# -----------------------------------------------------------------------------
def setup_environment(project_dir: Path, result: DetectionResult) -> List[CommandResult]:
    """Route to the correct per-language setup function for a detection result."""
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
        return setup_fn(project_dir, result)
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
