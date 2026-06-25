"""Self-healing install executor.

This is what makes DevReady "smart enough to fix things itself". When a
dependency-install command fails, instead of giving up DevReady:

  1. captures the real error output,
  2. tries cheap built-in retries first (pip's relaxed resolver, npm's
     ``--legacy-peer-deps``) — these fix the most common cases offline,
  3. then, if an OpenRouter key is configured, asks the LLM for a *structured*
     fix (install a missing system library, pin a conflicting version, set an
     env var, or adjust the command), applies it, and retries —
  4. looping until the install succeeds or no further fix is available.

Safety: the LLM never hands us free-form shell to execute blindly. It returns
typed actions, and any ``run`` command is validated against an allowlist of
package-manager heads and a denylist of destructive tokens before it runs. The
loop is bounded and never repeats an identical fix, so it can't spin forever.

Without an LLM key the healer still does the built-in retries, so behaviour is
strictly a superset of the old inline retry logic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from ..config import Config
from ..utils import CommandResult, console, run_command, run_command_teed

# How many LLM-suggested fixes to try before giving up on one install command.
MAX_HEAL_ATTEMPTS = 3

# Command "heads" the LLM is allowed to propose as a fix. Anything whose first
# word (after an optional ``sudo``) isn't here is rejected — we only let the
# healer install/build things, never run arbitrary programs.
_ALLOWED_HEADS = {
    # Python
    "pip", "pip3", "python", "python3", "py", "uv", "poetry", "pipenv", "conda", "mamba",
    # Node
    "npm", "npx", "yarn", "pnpm", "corepack", "node",
    # System package managers
    "apt", "apt-get", "brew", "choco", "winget", "scoop", "dnf", "yum", "pacman", "apk", "zypper",
    # Other ecosystems
    "gem", "bundle", "cargo", "rustup", "go", "dotnet", "composer", "php",
    # Env (harmless)
    "setx", "export", "set",
}

# Substrings that must never appear in a proposed command — destructive actions
# or "pipe-the-internet-into-a-shell" patterns. Checked case-insensitively.
_FORBIDDEN_TOKENS = (
    "rm ", "rm -", "rmdir", "del ", "erase ", "format ", "mkfs", "dd ", "fdisk",
    ":(){", "shutdown", "reboot", "deltree", "> /dev", "reg delete", "net user",
    "curl", "wget", "iwr", "invoke-webrequest", "irm", "invoke-restmethod",
    "| sh", "| bash", "| iex", "|iex", "-enc", "rd /s", "chmod 777 /", "chown -r",
)


@dataclass
class FixAction:
    """One typed fix the LLM proposed (and we validated)."""

    type: str  # "system_package" | "run" | "set_env" | "replace_install"
    name: str = ""          # system_package name, or env var name
    value: str = ""         # env var value
    command: str = ""       # run / replace_install command


@dataclass
class InstallHealer:
    """Runs install commands with built-in retries and LLM-guided self-healing.

    Construct one per ``devready start`` run and pass it into the environment
    setup. When ``config.llm`` isn't configured the LLM steps are skipped and
    only the offline retries run.
    """

    config: Config
    project_dir: Path
    assume_yes: bool = True
    _seen_fixes: set = field(default_factory=set)

    # -- public API ----------------------------------------------------------
    def run_step(
        self,
        command: Sequence[str],
        *,
        cwd: Optional[str] = None,
        description: str = "install",
    ) -> CommandResult:
        """Run an install command, healing and retrying on failure.

        Streams output live while capturing it (so we can diagnose a failure),
        applies built-in retries, then the LLM loop. Returns the final result.
        """
        cwd = cwd or str(self.project_dir)
        result = run_command_teed(command, cwd=cwd)
        if result.ok:
            return result

        # 1. Cheap, offline, high-hit-rate retries first.
        for retry in self._builtin_retries(command):
            console.print(
                f"  [warning]{description} failed — retrying: {' '.join(retry)}[/warning]"
            )
            result = run_command_teed(retry, cwd=cwd)
            if result.ok:
                return result

        # 2. LLM-guided healing loop (only if a key is configured).
        if self.config.llm.is_configured:
            result = self._heal_loop(list(command), cwd, result, description)
        return result

    # -- built-in (offline) retries ------------------------------------------
    @staticmethod
    def _builtin_retries(command: Sequence[str]) -> List[List[str]]:
        """Return cheap retry variants for a failed command, most-likely first."""
        cmd = list(command)
        joined = " ".join(cmd).lower()
        retries: List[List[str]] = []
        if "-m pip install" in joined or joined.startswith(("pip ", "pip3 ")):
            # Relaxed resolver fixes most "incompatible pin" failures.
            retries.append(cmd + ["--upgrade-strategy", "only-if-needed"])
        if joined.endswith(("npm install", "npm ci")) or " npm install" in joined:
            # Peer-dependency conflicts are common and this resolves them.
            base = [c for c in cmd if c != "ci"]
            if "install" not in base:
                base.append("install")
            retries.append(base + ["--legacy-peer-deps"])
        return retries

    # -- LLM healing loop ----------------------------------------------------
    def _heal_loop(
        self, command: List[str], cwd: str, last: CommandResult, description: str
    ) -> CommandResult:
        """Ask the LLM for fixes and retry, up to MAX_HEAL_ATTEMPTS times."""
        from .client import ask_llm_json

        current = command
        result = last
        for attempt in range(1, MAX_HEAL_ATTEMPTS + 1):
            console.print(
                f"  [info]Asking the AI to diagnose the failure "
                f"(attempt {attempt}/{MAX_HEAL_ATTEMPTS})…[/info]"
            )
            data = ask_llm_json(
                self.config,
                _HEAL_SYSTEM_PROMPT,
                self._diagnosis_prompt(current, result),
            )
            if not data:
                console.print("  [muted]AI couldn't be reached — keeping the original error.[/muted]")
                break

            diagnosis = str(data.get("diagnosis", "")).strip()
            if diagnosis:
                console.print(f"  [info]Diagnosis:[/info] {diagnosis}")
            if data.get("give_up"):
                console.print("  [muted]AI reports this isn't auto-fixable.[/muted]")
                break

            actions = self._parse_actions(data.get("actions"))
            if not actions:
                break

            replacement = self._apply_actions(actions, cwd)
            if replacement:
                current = replacement

            console.print(f"  [info]Retrying {description} after the fix…[/info]")
            result = run_command_teed(current, cwd=cwd)
            if result.ok:
                console.print("  [success]Recovered — the install succeeded after the AI fix.[/success]")
                return result

        return result

    def _diagnosis_prompt(self, command: Sequence[str], result: CommandResult) -> str:
        """Build the user message describing the failure for the LLM."""
        import platform

        # Send only the tail of the output — that's where the real error is, and
        # it keeps us inside free-tier context windows.
        error_tail = "\n".join(result.stdout.splitlines()[-60:])
        files = self._project_signature()
        return (
            f"OS: {platform.system()} ({platform.machine()})\n"
            f"Python running DevReady: {platform.python_version()}\n"
            f"Project files: {files}\n"
            f"Command that failed (exit {result.returncode}):\n  {' '.join(command)}\n\n"
            f"Error output (tail):\n{error_tail}\n"
        )

    def _project_signature(self) -> str:
        """A short list of key files so the LLM understands the stack."""
        names = [
            "requirements.txt", "pyproject.toml", "setup.py", "Pipfile",
            "package.json", "pnpm-lock.yaml", "yarn.lock",
            "Cargo.toml", "go.mod", "Gemfile", "composer.json", "pom.xml",
        ]
        present = [n for n in names if (self.project_dir / n).exists()]
        return ", ".join(present) or "unknown"

    # -- applying fixes ------------------------------------------------------
    def _parse_actions(self, raw) -> List[FixAction]:
        """Coerce the LLM's ``actions`` array into validated FixAction objects."""
        if not isinstance(raw, list):
            return []
        actions: List[FixAction] = []
        for item in raw[:MAX_HEAL_ATTEMPTS]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type", "")).strip()
            if kind == "system_package":
                name = str(item.get("name", "")).strip()
                if name:
                    actions.append(FixAction("system_package", name=name))
            elif kind == "set_env":
                name = str(item.get("name", "")).strip()
                if name:
                    actions.append(FixAction("set_env", name=name, value=str(item.get("value", ""))))
            elif kind in ("run", "replace_install"):
                cmd = str(item.get("command", "")).strip()
                if cmd and is_safe_command(cmd):
                    actions.append(FixAction(kind, command=cmd))
                elif cmd:
                    console.print(f"  [muted]Skipping an unsafe suggested command: {cmd}[/muted]")
        return actions

    def _apply_actions(self, actions: List[FixAction], cwd: str) -> Optional[List[str]]:
        """Apply each fix. Returns a replacement install command, if one was given."""
        from ..environment import system_deps

        replacement: Optional[List[str]] = None
        for action in actions:
            signature = f"{action.type}:{action.name}:{action.command}"
            if signature in self._seen_fixes:
                continue  # never apply the same fix twice
            self._seen_fixes.add(signature)

            if action.type == "system_package":
                console.print(f"  [info]Installing missing system dependency: {action.name}[/info]")
                system_deps.ensure_packages([action.name], assume_yes=True)
            elif action.type == "set_env":
                console.print(f"  [info]Setting {action.name} for this run.[/info]")
                os.environ[action.name] = action.value
            elif action.type == "run":
                console.print(f"  [info]Running fix: {action.command}[/info]")
                run_command_teed(action.command, cwd=cwd, shell=True)
            elif action.type == "replace_install":
                console.print(f"  [info]Adjusting the install command: {action.command}[/info]")
                replacement = action.command.split()
        return replacement


# -----------------------------------------------------------------------------
# Command-safety validation (module-level so it's easy to unit test)
# -----------------------------------------------------------------------------
def is_safe_command(command: str) -> bool:
    """Return True if a proposed fix command is safe to run automatically.

    A command is safe only when its head (after an optional ``sudo``) is a known
    package-manager / build tool AND it contains no destructive or
    pipe-to-shell tokens. Conservative on purpose: we'd rather reject a valid
    fix than ever run something harmful unattended.
    """
    if not command or not command.strip():
        return False
    low = command.lower()
    if any(token in low for token in _FORBIDDEN_TOKENS):
        return False
    parts = command.split()
    head = parts[0]
    if head == "sudo":
        if len(parts) < 2:
            return False
        head = parts[1]
    head = Path(head).name.lower()  # strip any path, normalise case
    # Strip a trailing .exe/.cmd on Windows-style heads.
    for suffix in (".exe", ".cmd", ".bat"):
        if head.endswith(suffix):
            head = head[: -len(suffix)]
    return head in _ALLOWED_HEADS


_HEAL_SYSTEM_PROMPT = (
    "You are DevReady's install troubleshooter. A dependency-install command "
    "failed. Using the command, its error output, the OS, and the project files, "
    "determine the smallest safe fix and return ONLY a JSON object with exactly "
    "these keys:\n"
    '  "diagnosis": one short sentence naming the root cause,\n'
    '  "give_up": boolean — true if this cannot be fixed automatically,\n'
    '  "actions": an array (max 3) of fix steps, each one of:\n'
    '     {"type": "system_package", "name": "<os package, e.g. ffmpeg>"}\n'
    '     {"type": "set_env", "name": "<VAR>", "value": "<value>"}\n'
    '     {"type": "run", "command": "<a safe install/build command>"}\n'
    '     {"type": "replace_install", "command": "<a corrected install command to retry>"}\n'
    "Rules: only suggest non-destructive commands that install packages, pin "
    "versions, or set env vars (e.g. pip, npm, apt, brew, choco, cargo). Never "
    "suggest deleting files, downloading-and-piping to a shell, or anything "
    "outside building/installing. Prefer the smallest fix. If a package fails to "
    "build because no wheel exists for this Python version, suggest pinning a "
    "compatible version via replace_install. Use give_up=true when unsure."
)
