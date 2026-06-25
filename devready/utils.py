"""Shared helpers used across DevReady.

This module centralises the small, generic utilities that don't belong to any
single feature: the shared Rich console, a safe subprocess runner, OS / package
manager detection, and a few formatting helpers.

Keeping these in one place means the rest of the codebase can stay focused on
*what* it does rather than *how* to print or run things.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
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


def _resolve_windows_executable(command: Sequence[str] | str) -> Sequence[str] | str:
    """Resolve a bare tool name to its full path on Windows.

    Console tools like ``npm``/``npx``/``yarn``/``pnpm`` are ``.cmd``/``.bat``
    shims on Windows. Python's ``subprocess`` (without a shell) can't launch them
    from a bare name — CreateProcess only resolves ``.exe``/``.com``, so
    ``["npm", "install"]`` raises FileNotFoundError even though npm is installed.
    Resolving the real path (which honours PATHEXT and finds ``npm.cmd``) makes
    these run correctly. Harmless for ``.exe`` targets and for absolute paths.
    """
    if sys.platform != "win32" or isinstance(command, str) or not command:
        return command
    resolved = shutil.which(command[0])
    return [resolved, *command[1:]] if resolved else command


def run_command(
    command: Sequence[str] | str,
    *,
    cwd: Optional[str] = None,
    capture: bool = True,
    shell: bool = False,
    timeout: Optional[int] = None,
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
    if not shell:
        command = _resolve_windows_executable(command)
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=shell,
            capture_output=capture,
            text=True,  # decode bytes to str using the default encoding
            timeout=timeout,
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
) -> CommandResult:
    """Run a command, streaming its output live AND capturing the tail.

    Like :func:`run_command`, but the child's combined stdout+stderr is both
    shown to the user in real time (important for slow installs like a multi-
    minute ``pip install torch``) and captured, so a failure can be diagnosed —
    e.g. handed to the LLM healer. Only the last ``max_capture_lines`` lines are
    retained, which is more than enough for an error trace while keeping memory
    bounded on a chatty build.

    Returns a :class:`CommandResult` whose ``stdout`` holds the captured tail.
    Never raises on a non-zero exit; the caller decides what failure means.
    """
    from collections import deque

    display = command if isinstance(command, str) else " ".join(command)
    if not shell:
        command = _resolve_windows_executable(command)

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
        )
    except FileNotFoundError:
        return CommandResult(command=display, returncode=127, stderr="command not found")

    try:
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            captured.append(line)
        sys.stdout.flush()
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        return CommandResult(
            command=display, returncode=124, stderr="timed out", stdout="".join(captured)
        )

    return CommandResult(
        command=display, returncode=process.returncode, stdout="".join(captured)
    )


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
