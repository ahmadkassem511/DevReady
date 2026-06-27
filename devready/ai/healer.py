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
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

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
        env: Optional[dict] = None,
    ) -> CommandResult:
        """Run an install command, healing and retrying on failure.

        Streams output live while capturing it (so we can diagnose a failure),
        applies built-in retries, then the LLM loop. ``env`` is forwarded to the
        subprocess (used to run a pinned-version runtime's tools). Returns the
        final result.
        """
        cwd = cwd or str(self.project_dir)
        result = run_command_teed(command, cwd=cwd, env=env)
        if result.ok:
            return result

        # 1. Cheap, offline, high-hit-rate retries first. These cover the most
        #    common real-world failures deterministically — no LLM needed.
        for retry in self._builtin_retries(command, result.stdout):
            console.print(
                f"  [warning]{description} failed — retrying: {' '.join(retry)}[/warning]"
            )
            result = run_command_teed(retry, cwd=cwd, env=env)
            if result.ok:
                return result

        # 2. Resilient requirements install: if a single dependency can't be built
        #    or found (e.g. a CUDA/compiler package like flash_attn on a CPU box),
        #    install everything ELSE and skip the offender — so the project still
        #    gets set up instead of failing wholesale.
        interpreter, req_file = self._pip_requirements_target(command)
        if req_file:
            result = self._resilient_pip_install(interpreter, req_file, cwd, env, result)
            if result.ok:
                return result

        # 3. LLM-guided healing loop (only if a key is configured). Fixes run in
        #    the project's interpreter, not a global one.
        if self.config.llm.is_configured:
            result = self._heal_loop(list(command), cwd, result, description, env, interpreter)
        return result

    # -- resilient requirements install --------------------------------------
    @staticmethod
    def _pip_requirements_target(command: Sequence[str]) -> Tuple[Optional[str], Optional[str]]:
        """If ``command`` is a ``pip install -r <file>``, return (interpreter, file).

        ``interpreter`` is the venv python when the command is
        ``<py> -m pip install -r <file>`` (so retries stay in the venv); else None.
        Returns (None, None) when it isn't a requirements install.
        """
        cmd = list(command)
        lower = [c.lower() for c in cmd]
        if "pip" not in " ".join(lower) or "install" not in lower or "-r" not in cmd:
            return (None, None)
        interpreter = cmd[0] if len(cmd) > 2 and cmd[1] == "-m" and cmd[2] == "pip" else None
        try:
            req_file = cmd[cmd.index("-r") + 1]
        except (ValueError, IndexError):
            return (None, None)
        return (interpreter, req_file)

    @staticmethod
    def _requirement_name(line: str) -> Optional[str]:
        """Extract a normalised package name from a requirements line, or None."""
        s = line.strip()
        if not s or s.startswith(("#", "-")):
            return None
        name = re.split(r"[<>=!~;\[ ]", s, maxsplit=1)[0].strip()
        return name.lower().replace("_", "-") or None

    def _failing_package(self, error_text: str, lines: List[str], active: List[int]) -> Optional[int]:
        """Return the index of the active requirement line pip failed on, or None."""
        text = (error_text or "").lower()
        patterns = (
            r"failed to build '?([a-z0-9_.\-]+)'?",
            r"could not build wheels? for ([a-z0-9_.\-]+)",
            r"failed building wheel for ([a-z0-9_.\-]+)",
            r"no matching distribution found for ([a-z0-9_.\-]+)",
            r"could not find a version that satisfies the requirement ([a-z0-9_.\-]+)",
            r"error: could not build wheels for ([a-z0-9_.\-]+)",
        )
        names = set()
        for pat in patterns:
            for m in re.findall(pat, text):
                names.add(m.strip().replace("_", "-"))
        if not names:
            return None
        for i in active:
            pn = self._requirement_name(lines[i])
            if pn and pn in names:
                return i
        return None

    def _resilient_pip_install(
        self, interpreter: Optional[str], req_file: str, cwd: str,
        env: Optional[dict], last_result: CommandResult,
    ) -> CommandResult:
        """Install a requirements file, dropping packages that can't be built/found.

        Iteratively: find the package pip failed on, remove it, reinstall the rest.
        Lets a heavy ML repo install 13/14 deps instead of 0 when one package
        (flash_attn, deepspeed, …) can't compile on this machine.
        """
        req_path = Path(req_file)
        if not req_path.is_absolute():
            req_path = Path(cwd) / req_file
        try:
            lines = req_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return last_result

        active = [i for i, line in enumerate(lines) if self._requirement_name(line) is not None]
        if not active:
            return last_result

        pip_base = [interpreter, "-m", "pip"] if interpreter else ["pip"]
        result = last_result
        skipped: List[str] = []

        for _ in range(len(active) + 1):
            bad = self._failing_package(result.stdout, lines, active)
            if bad is None:
                break  # can't pinpoint a culprit to drop — leave it to the LLM
            name = self._requirement_name(lines[bad]) or lines[bad].strip()
            skipped.append(name)
            active = [i for i in active if i != bad]
            console.print(
                f"  [warning]Couldn't install '{name}' — skipping it and installing the rest…[/warning]"
            )
            if not active:
                break
            tmp = Path(tempfile.gettempdir()) / f"devready-req-{os.getpid()}.txt"
            tmp.write_text("\n".join(lines[i] for i in active) + "\n", encoding="utf-8")
            result = run_command_teed(pip_base + ["install", "-r", str(tmp)], cwd=cwd, env=env)
            if result.ok:
                break

        if skipped and result.ok:
            console.print(
                f"  [success]Installed all dependencies except: {', '.join(skipped)}.[/success]\n"
                f"  [muted]Those usually need a GPU/compiler and are optional — the project "
                f"should still run; install them manually if you need them.[/muted]"
            )
        return result

    # -- built-in (offline) retries ------------------------------------------
    def _builtin_retries(self, command: Sequence[str], error_text: str = "") -> List[List[str]]:
        """Return cheap retry variants for a failed command, most-likely first.

        These encode the well-known escape hatches for the dependency managers so
        DevReady fixes the common cases itself:
          * pip — relax the resolver when a pin conflict blocks the install.
          * npm — when ``npm ci`` fails (a stale/desynced lockfile is extremely
            common), fall back to ``npm install`` to regenerate it; when a
            lifecycle script fails (e.g. a Unix ``postinstall`` shell script on
            Windows), retry with ``--ignore-scripts`` so dependencies still land;
            and ``--legacy-peer-deps`` for peer-dependency conflicts.
        Any retry identical to the original command is dropped.
        """
        cmd = list(command)
        joined = " ".join(cmd).lower()
        retries: List[List[str]] = []

        if "-m pip install" in joined or joined.startswith(("pip ", "pip3 ")):
            retries.append(cmd + ["--upgrade-strategy", "only-if-needed"])

        if "npm" in joined and ("install" in joined or "ci" in joined):
            # Normalise `npm ci` → `npm install`: ci aborts on any lockfile
            # mismatch, whereas install repairs the lockfile.
            base = ["install" if part == "ci" else part for part in cmd]
            if "install" not in base:
                base.append("install")
            # Escalating fallbacks, broadest-but-safest last. --ignore-scripts is
            # the key one for repos whose postinstall runs a Unix shell script.
            retries.append(base)
            retries.append(base + ["--ignore-scripts"])
            retries.append(base + ["--legacy-peer-deps"])
            retries.append(base + ["--legacy-peer-deps", "--ignore-scripts", "--no-audit", "--no-fund"])

        # Drop any retry that is just the original command, and de-duplicate.
        unique: List[List[str]] = []
        for r in retries:
            if r != cmd and r not in unique:
                unique.append(r)
        return unique

    # -- LLM healing loop ----------------------------------------------------
    def _heal_loop(
        self,
        command: List[str],
        cwd: str,
        last: CommandResult,
        description: str,
        env: Optional[dict] = None,
        interpreter: Optional[str] = None,
    ) -> CommandResult:
        """Ask the LLM for fixes and retry, up to MAX_HEAL_ATTEMPTS times.

        ``interpreter`` is the project's venv python (when known) so a suggested
        ``pip install …`` / ``python …`` fix runs INSIDE the project's
        environment, not a global one.
        """
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

            replacement = self._apply_actions(actions, cwd, env, interpreter)
            if replacement:
                current = replacement

            console.print(f"  [info]Retrying {description} after the fix…[/info]")
            result = run_command_teed(current, cwd=cwd, env=env)
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

    @staticmethod
    def _venv_rewrite(cmd_str: str, interpreter: Optional[str]) -> Optional[List[str]]:
        """Rewrite a bare ``pip``/``python`` fix to use the project's interpreter.

        The AI often suggests ``pip install X`` — but bare ``pip`` is the GLOBAL
        one, so the fix lands in the wrong environment and does nothing. When we
        know the venv interpreter, route it there. Returns the argv list, or None
        when there's nothing to rewrite (run the command as given).
        """
        if not interpreter:
            return None
        parts = cmd_str.split()
        if not parts:
            return None
        head = Path(parts[0]).name.lower()
        for suffix in (".exe", ".cmd", ".bat"):
            if head.endswith(suffix):
                head = head[: -len(suffix)]
        if head in ("pip", "pip3"):
            return [interpreter, "-m", "pip"] + parts[1:]
        if head in ("python", "python3", "py"):
            return [interpreter] + parts[1:]
        return None

    def _apply_actions(
        self, actions: List[FixAction], cwd: str, env: Optional[dict] = None,
        interpreter: Optional[str] = None,
    ) -> Optional[List[str]]:
        """Apply each fix. Returns a replacement install command, if one was given.

        ``env`` is forwarded so a ``run`` fix uses the same (possibly pinned)
        toolchain as the install it's repairing. ``interpreter`` (the venv python)
        ensures ``pip``/``python`` fixes run INSIDE the project's environment, not
        a global one — without this, every AI fix silently targets the wrong env.
        """
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
                if env is not None:
                    env[action.name] = action.value
            elif action.type == "run":
                rewritten = self._venv_rewrite(action.command, interpreter)
                if rewritten:
                    console.print(f"  [info]Running fix (in the project's env): {' '.join(rewritten)}[/info]")
                    run_command_teed(rewritten, cwd=cwd, env=env)
                else:
                    console.print(f"  [info]Running fix: {action.command}[/info]")
                    run_command_teed(action.command, cwd=cwd, shell=True, env=env)
            elif action.type == "replace_install":
                console.print(f"  [info]Adjusting the install command: {action.command}[/info]")
                replacement = self._venv_rewrite(action.command, interpreter) or action.command.split()
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
