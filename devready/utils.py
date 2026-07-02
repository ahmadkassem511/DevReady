"""Shared helpers used across DevReady.

This module centralises the small, generic utilities that don't belong to any
single feature: the shared Rich console, a safe subprocess runner, OS / package
manager detection, and a few formatting helpers.

Keeping these in one place means the rest of the codebase can stay focused on
*what* it does rather than *how* to print or run things.
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from rich.console import Console
from rich.panel import Panel
from rich.theme import Theme

# Windows' legacy console defaults to a codepage (e.g. cp1252) that can't encode
# emoji or em-dashes, which makes Rich raise UnicodeEncodeError. Reconfiguring
# the streams to UTF-8 with error replacement keeps DevReady from ever crashing
# on output, regardless of the terminal. This is a no-op on platforms/streams
# that are already UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

# -----------------------------------------------------------------------------
# Shared console
# -----------------------------------------------------------------------------
# We create a SINGLE Rich Console and import it everywhere. Rich recommends a
# single console per application so styling, width detection, and recording all
# behave consistently. Custom theme keeps colour names meaningful in the code.
_theme = Theme(
    {
        "info": "cyan",
        "success": "bold green",
        "warning": "bold yellow",
        "error": "bold red",
        "step": "bold magenta",
        "muted": "dim",
    }
)
console = Console(theme=_theme)


# -----------------------------------------------------------------------------
# Command execution
# -----------------------------------------------------------------------------
@dataclass
class CommandResult:
    """Outcome of a shell command.

    Attributes:
        command: The exact command that was run (joined for display).
        returncode: Process exit code. 0 conventionally means success.
        stdout: Captured standard output (empty if streamed to terminal).
        stderr: Captured standard error (empty if streamed to terminal).
    """

    command: str
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        """True when the command exited successfully."""
        return self.returncode == 0


def git_bash() -> Optional[str]:
    """Locate a *real* Git Bash, never ``C:\\Windows\\System32\\bash.exe``.

    System32's ``bash.exe`` is the WSL launcher; on a machine with no WSL distro
    it dies with ``execvpe(/bin/bash) failed`` / ``HCS_E_SERVICE_NOT_AVAILABLE``.
    Since System32 is usually early on PATH, a bare ``which("bash")`` finds that
    stub — so we resolve Git Bash from git's own location / standard install dirs.
    On non-Windows, ``bash`` on PATH is correct.
    """
    if sys.platform != "win32":
        return shutil.which("bash")
    candidate = shutil.which("bash")
    if candidate and "system32" not in candidate.lower():
        return candidate
    roots: list = []
    git = shutil.which("git")
    if git:
        p = Path(git).resolve()
        roots += [p.parent.parent, p.parent.parent.parent]  # …/Git/cmd|mingw64/bin/git.exe → …/Git
    roots += [
        Path(r"C:\Program Files\Git"),
        Path(r"C:\Program Files (x86)\Git"),
        Path(os.path.expanduser(r"~\AppData\Local\Programs\Git")),
        Path(os.path.expanduser(r"~\scoop\apps\git\current")),
    ]
    for root in roots:
        for rel in ("bin/bash.exe", "usr/bin/bash.exe"):
            bash = root / rel
            try:
                if bash.exists():
                    return str(bash)
            except OSError:
                continue
    return None


def _resolve_windows_executable(
    command: Sequence[str] | str, path: Optional[str] = None
) -> Sequence[str] | str:
    """Resolve a bare tool name to its full path on Windows.

    Console tools like ``npm``/``npx``/``yarn``/``pnpm`` are ``.cmd``/``.bat``
    shims on Windows. Python's ``subprocess`` (without a shell) can't launch them
    from a bare name — CreateProcess only resolves ``.exe``/``.com``, so
    ``["npm", "install"]`` raises FileNotFoundError even though npm is installed.
    Resolving the real path (which honours PATHEXT and finds ``npm.cmd``) makes
    these run correctly. Harmless for ``.exe`` targets and for absolute paths.

    ``bash``/``sh`` are special-cased to a real Git Bash (never the System32 WSL
    stub), so a project's ``setup.sh`` and any shell command run correctly.

    ``path`` overrides which PATH to search — used when running a tool from a
    specific runtime (e.g. an fnm-managed Node's bin dir) that isn't the default.
    """
    if sys.platform != "win32" or isinstance(command, str) or not command:
        return command
    base = command[0].lower().replace("\\", "/").split("/")[-1]
    if base in ("bash", "bash.exe", "sh", "sh.exe"):
        gb = git_bash()
        if gb:
            return [gb, *command[1:]]
    resolved = shutil.which(command[0], path=path)
    return [resolved, *command[1:]] if resolved else command


def run_command(
    command: Sequence[str] | str,
    *,
    cwd: Optional[str] = None,
    capture: bool = True,
    shell: bool = False,
    timeout: Optional[int] = None,
    env: Optional[dict] = None,
) -> CommandResult:
    """Run an external command safely and return a structured result.

    This is the ONE place DevReady shells out to the system. Routing every
    external call through here gives us consistent error handling, optional
    output capture, and a single spot to add logging or dry-run support later.

    Args:
        command: Either a list of args (preferred — avoids shell quoting bugs)
            or a string when ``shell=True``.
        cwd: Working directory to run in. Defaults to the current directory.
        capture: When True, capture stdout/stderr instead of streaming them.
            Set False for long-running commands whose live output the user
            should see (e.g. ``npm install``).
        shell: Run through the system shell. Avoid when possible — only use it
            for commands that genuinely need shell features.
        timeout: Optional seconds before the command is killed.

    Returns:
        A :class:`CommandResult`. We never raise on a non-zero exit code; the
        caller decides what a failure means in context.
    """
    display = command if isinstance(command, str) else " ".join(command)
    # On Windows, resolve .cmd/.bat shims (npm, npx, yarn…) to a launchable path.
    # When a custom env is given, search its PATH so a tool from that runtime
    # (e.g. an fnm-managed Node's corepack) resolves correctly.
    if not shell:
        command = _resolve_windows_executable(command, path=(env or {}).get("PATH"))
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=shell,
            capture_output=capture,
            text=True,  # decode bytes to str using the default encoding
            timeout=timeout,
            env=env,
        )
        return CommandResult(
            command=display,
            returncode=completed.returncode,
            stdout=(completed.stdout or "") if capture else "",
            stderr=(completed.stderr or "") if capture else "",
        )
    except FileNotFoundError:
        # The executable itself wasn't found (e.g. `npm` not installed).
        return CommandResult(command=display, returncode=127, stderr="command not found")
    except subprocess.TimeoutExpired:
        return CommandResult(command=display, returncode=124, stderr="timed out")


def run_command_teed(
    command: "Sequence[str] | str",
    *,
    cwd: Optional[str] = None,
    shell: bool = False,
    timeout: Optional[int] = None,
    max_capture_lines: int = 400,
    env: Optional[dict] = None,
    heartbeat_secs: int = 45,
) -> CommandResult:
    """Run a command, streaming its output live AND capturing the tail.

    Like :func:`run_command`, but the child's combined stdout+stderr is both
    shown to the user in real time (important for slow installs like a multi-
    minute ``pip install torch``) and captured, so a failure can be diagnosed —
    e.g. handed to the LLM healer. Only the last ``max_capture_lines`` lines are
    retained, which is more than enough for an error trace while keeping memory
    bounded on a chatty build.

    ``heartbeat_secs``: when the child produces no output for this long, print a
    liveness note (with elapsed time). Heavy builds — e.g. ``pip install .`` that
    compiles a frontend, or a big wheel — can be silent for many minutes; without
    this the run looks frozen, especially in the GUI whose only signal is the
    streamed log. Set to 0 to disable.

    Returns a :class:`CommandResult` whose ``stdout`` holds the captured tail.
    Never raises on a non-zero exit; the caller decides what failure means.
    """
    from collections import deque

    display = command if isinstance(command, str) else " ".join(command)
    if not shell:
        command = _resolve_windows_executable(command, path=(env or {}).get("PATH"))

    captured: "deque[str]" = deque(maxlen=max_capture_lines)
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge so the error context stays in order
            text=True,
            bufsize=1,  # line-buffered, so live output isn't withheld
            errors="replace",
            env=env,
        )
    except FileNotFoundError:
        return CommandResult(command=display, returncode=127, stderr="command not found")

    # Heartbeat watchdog: the read loop below blocks while the child is silent, so
    # a separate thread emits "still working" notes so a long quiet build doesn't
    # look frozen. It writes to stdout (which the GUI streams) and never touches
    # the captured tail, so diagnosis output is unchanged.
    start = time.time()
    last_output = [start]
    stop = threading.Event()

    def _heartbeat() -> None:
        while not stop.wait(min(heartbeat_secs, 10)):
            if time.time() - last_output[0] >= heartbeat_secs:
                mins = int((time.time() - start) // 60)
                elapsed = f"{mins} min" if mins else "under a minute"
                sys.stdout.write(
                    f"  … still working — {elapsed} elapsed (large builds/downloads "
                    f"can be quiet for a while; please keep waiting)\n"
                )
                sys.stdout.flush()
                last_output[0] = time.time()  # wait another full interval before the next note

    hb = None
    if heartbeat_secs and heartbeat_secs > 0:
        hb = threading.Thread(target=_heartbeat, daemon=True)
        hb.start()

    try:
        assert process.stdout is not None
        for line in process.stdout:
            last_output[0] = time.time()
            sys.stdout.write(line)
            captured.append(line)
        sys.stdout.flush()
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop.set()
        process.kill()
        return CommandResult(
            command=display, returncode=124, stderr="timed out", stdout="".join(captured)
        )
    finally:
        stop.set()

    return CommandResult(
        command=display, returncode=process.returncode, stdout="".join(captured)
    )


def force_rmtree(path: "Path | str", attempts: int = 3) -> bool:
    """Recursively delete a directory tree, robustly. Returns True if it's gone.

    Plain ``shutil.rmtree`` routinely fails on Windows because Git writes its
    ``.git/objects`` pack files as **read-only** — rmtree hits a PermissionError
    partway and leaves the folder behind (which then makes a fresh ``git clone``
    into it fail with "already exists and is not empty"). This proactively clears
    the read-only bit on every entry first, then deletes, and retries a few times
    to outlast transient locks from antivirus / the search indexer.
    """
    target = Path(path)
    # Full owner permissions (rwx). On Windows any write bit clears the
    # read-only attribute; on POSIX directories must KEEP read+execute or
    # rmtree can no longer list/traverse them (bare S_IWRITE = 0o200 would
    # brick the tree we're trying to delete).
    writable = stat.S_IRWXU
    for _ in range(max(1, attempts)):
        if not target.exists():
            return True
        # Clear read-only across the whole tree so nothing can block removal.
        for root, dirs, files in os.walk(target):
            for name in dirs + files:
                try:
                    os.chmod(os.path.join(root, name), writable)
                except OSError:
                    pass
        try:
            os.chmod(target, writable)
        except OSError:
            pass
        shutil.rmtree(target, ignore_errors=True)
        if not target.exists():
            return True
        time.sleep(0.5)  # let an AV/indexer release its handle, then retry
    return not target.exists()


def command_exists(name: str) -> bool:
    """Return True if an executable is available on the user's PATH.

    Used everywhere we need to know whether a tool (pyenv, nvm, docker, brew…)
    is installed before trying to use it.
    """
    return shutil.which(name) is not None


# -----------------------------------------------------------------------------
# Operating system / package manager detection
# -----------------------------------------------------------------------------
def get_os() -> str:
    """Return a normalised OS name: 'macos', 'linux', or 'windows'."""
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    return "linux"


def detect_package_manager() -> Optional[str]:
    """Detect the system package manager for installing OS-level dependencies.

    Returns the name of the first manager found, or None if none is available.
    The order reflects what's idiomatic on each platform.
    """
    candidates = {
        "macos": ["brew"],
        "linux": ["apt", "apt-get", "dnf", "yum", "pacman"],
        "windows": ["choco", "winget", "scoop"],
    }
    for manager in candidates.get(get_os(), []):
        if command_exists(manager):
            return manager
    return None


# -----------------------------------------------------------------------------
# Output helpers
# -----------------------------------------------------------------------------
def print_banner(text: str) -> None:
    """Print a prominent titled panel — used for the welcome / step headers."""
    console.print(Panel.fit(text, border_style="step"))


def print_step(number: int, total: int, title: str) -> None:
    """Print a numbered step header, e.g. ``[2/8] README Analysis``."""
    console.print(f"\n[step]\\[{number}/{total}][/step] [bold]{title}[/bold]")
