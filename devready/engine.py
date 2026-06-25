"""The orchestration engine — the brain behind ``devready start``.

:class:`Engine` runs the eight-step setup pipeline in order and also implements
the supporting commands (``status``, ``stop``, ``clean``, ``doctor``). The CLI
layer (``cli.py``) is intentionally thin: it parses arguments and delegates to
the methods here, so all real logic lives in one place and is easy to test.

State that must survive between invocations (e.g. the PID of a launched server)
is stored in ``<project>/.devready/state.json``.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import socket
import sys
import time
import webbrowser
from pathlib import Path
from typing import List, Optional, Tuple

from rich.table import Table

from .ai import ReadmeInsights, parse_readme
from .config import Config, list_projects, register_project
from .detectors import DetectionResult, detect_stack
from .environment import env_vars, strategies, system_deps, version_manager
from .utils import (
    _resolve_windows_executable,
    command_exists,
    console,
    print_banner,
    print_step,
    run_command,
)

# Total number of pipeline steps, used for the "[n/TOTAL]" headers.
TOTAL_STEPS = 8


class Engine:
    """Coordinates project detection, setup, and launch for one project dir."""

    def __init__(
        self,
        project_dir: Optional[Path] = None,
        config: Optional[Config] = None,
        assume_yes: bool = False,
    ):
        # Default to the current working directory; resolve to an absolute path
        # so state files and subprocesses behave predictably.
        self.project_dir = (project_dir or Path.cwd()).resolve()
        self.config = config or Config.load()
        # When True (devready start --yes), proceed through every prompt with the
        # default answer — fully unattended. The user opted into running
        # repo-provided setup without per-step confirmation.
        self.assume_yes = assume_yes

        # Populated as the pipeline runs; later steps read these.
        self.detections: List[DetectionResult] = []
        self.insights: ReadmeInsights = ReadmeInsights()
        self._install_ok: bool = True  # set False if a dep-install step fails
        self._project_setup_ran: bool = False  # True if the project's own setup ran
        self._attempted_commands: set = set()  # launch commands already tried this run

    def _make_healer(self, project_dir: Path):
        """Build a self-healing install executor for a directory.

        The healer streams + captures install output, retries common failures
        offline, and (when an OpenRouter key is configured) asks the LLM for a
        safe fix and retries — so an install isn't abandoned at the first error.
        """
        from .ai.healer import InstallHealer

        return InstallHealer(self.config, project_dir, assume_yes=self.assume_yes)

    def _confirm(self, prompt: str, default_yes: bool = True) -> bool:
        """Ask a yes/no question, or auto-answer when running with --yes.

        ``prompt`` should end with the choice hint, e.g. "Proceed? [Y/n] ".
        """
        if self.assume_yes:
            return True
        answer = console.input(prompt).strip().lower()
        if default_yes:
            return answer in ("", "y", "yes")
        return answer in ("y", "yes")

    # =========================================================================
    # Internal state persistence
    # =========================================================================
    @property
    def _state_dir(self) -> Path:
        return self.project_dir / ".devready"

    @property
    def _state_file(self) -> Path:
        return self._state_dir / "state.json"

    def _write_state(self, **fields) -> None:
        """Merge ``fields`` into the persisted state file."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        state = self._read_state()
        state.update(fields)
        self._state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _state_processes(self, state: dict) -> List[dict]:
        """Return launched-process records, upgrading the legacy single-PID format.

        Older state files (before multi-process launch) stored a single ``pid`` /
        ``start_command`` / ``port`` at the top level; we normalise that into the
        same list shape the rest of the code expects.
        """
        processes = state.get("processes")
        if processes:
            return processes
        if state.get("pid") or state.get("start_command"):
            return [{
                "name": "root",
                "pid": state.get("pid"),
                "command": state.get("start_command", []),
                "port": state.get("port"),
                "cwd": str(self.project_dir),
            }]
        return []

    def _read_state(self) -> dict:
        """Read the persisted state, returning {} if none exists."""
        if self._state_file.exists():
            try:
                return json.loads(self._state_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    # =========================================================================
    # Public command: start (the full pipeline)
    # =========================================================================
    def start(self) -> bool:
        """Run the complete setup pipeline.

        Returns True when setup completed cleanly, False when a dependency
        install step failed (so callers — the CLI, and the GUI subprocess — can
        surface a real failure instead of a misleading success).
        """
        print_banner("[bold cyan]DevReady[/bold cyan] — getting your project ready 🚀")
        console.print(f"[muted]Project: {self.project_dir}[/muted]")

        # Record this project so `devready list` can show it later.
        register_project(self.project_dir)

        self._step_detect()
        self._step_analyze_readme()
        self._print_plan()  # complete plan (toolchains + packages + env) before any install
        self._step_system_deps()
        self._step_environment()
        self._step_env_vars()
        self._step_docker()
        self._step_migrations()
        self._step_launch()

        return self._install_ok

    # =========================================================================
    # Public command: run (fast relaunch, no setup)
    # =========================================================================
    def run(self) -> None:
        """Relaunch an already-set-up project without re-running setup.

        Uses the start command saved during the last ``devready start``. This is
        the everyday "just run my project again" command — instant, because it
        skips detection, installs, and all the other setup steps.

        If the project was never set up (no saved command), we fall back to
        detecting a start command on the fly; if even that fails, we point the
        user at ``devready start``.
        """
        print_banner("[bold cyan]DevReady[/bold cyan] — launching your project ▶")
        console.print(f"[muted]Project: {self.project_dir}[/muted]\n")

        state = self._read_state()
        saved = self._state_processes(state)

        # If anything's already running, don't start a duplicate.
        alive = [p for p in saved if p.get("pid") and _pid_alive(p["pid"])]
        if alive:
            console.print("  [success]Already running:[/success]")
            for proc in alive:
                port = proc.get("port")
                where = f" → http://localhost:{port}" if port else ""
                console.print(f"    • [bold]{proc.get('name', 'server')}[/bold]{where}")
            console.print("  Use [bold]devready stop[/bold] first if you want to restart it.")
            return

        # Relaunch the components saved by the last `start`.
        if saved:
            targets = [
                {"name": p.get("name", "root"),
                 "cwd": p.get("cwd", str(self.project_dir)),
                 "command": p["command"],
                 "port": p.get("port")}
                for p in saved if p.get("command")
            ]
            if targets:
                served = self._launch_targets(targets)
                if not served:
                    self._no_server_help()  # explain how to use it (no served URL)
                return

        # No saved launch — detect one on the fly without a full setup.
        console.print(
            "  [muted]No saved launch command — detecting one "
            "(run [bold]devready start[/bold] for a full setup).[/muted]"
        )
        self.detections = detect_stack(self.project_dir)
        targets = self._collect_launch_targets()
        served = self._launch_targets(targets) if targets else []
        if not served:
            self._no_server_help()

    # -- Step 1: Project detection -------------------------------------------
    def _step_detect(self) -> None:
        print_step(1, TOTAL_STEPS, "Project Detection")
        self.detections = detect_stack(self.project_dir)

        if not self.detections:
            console.print(
                "  [warning]Could not identify the stack. Continuing with README "
                "hints only.[/warning]"
            )
            return

        # Render a tidy summary table of what we found.
        table = Table(show_header=True, header_style="bold")
        table.add_column("Language")
        table.add_column("Version")
        table.add_column("Frameworks")
        table.add_column("Files")
        for det in self.detections:
            table.add_row(
                det.language,
                det.version or "[muted]any[/muted]",
                ", ".join(det.frameworks) or "[muted]—[/muted]",
                ", ".join(det.package_files),
            )
        console.print(table)

    # -- Step 2: README analysis ---------------------------------------------
    def _step_analyze_readme(self) -> None:
        print_step(2, TOTAL_STEPS, "README Analysis")

        readme = self._find_readme()
        if readme is None:
            console.print("  [muted]No README found — skipping analysis.[/muted]")
            return

        mode = "AI (OpenRouter)" if self.config.llm.is_configured else "offline regex parser"
        console.print(f"  Reading {readme.name} using the {mode}…")
        self.insights = parse_readme(readme.read_text(encoding="utf-8"), self.config)

        if self.insights.is_empty:
            console.print("  [muted]No setup instructions extracted.[/muted]")
            return

        if self.insights.commands:
            console.print(f"  Found [bold]{len(self.insights.commands)}[/bold] setup command(s).")
        if self.insights.system_packages:
            console.print(
                f"  Found [bold]{len(self.insights.system_packages)}[/bold] system package(s)."
            )
        if self.insights.env_vars:
            console.print(f"  Found [bold]{len(self.insights.env_vars)}[/bold] env var(s).")

    def _find_readme(self) -> Optional[Path]:
        """Locate a README file regardless of casing/extension."""
        for name in ("README.md", "README.rst", "README.txt", "README"):
            candidate = self.project_dir / name
            if candidate.exists():
                return candidate
        return None

    # -- Step 3: System dependency install -----------------------------------
    def _step_system_deps(self) -> None:
        print_step(3, TOTAL_STEPS, "System Dependencies")
        packages = self.insights.system_packages
        if not packages:
            console.print("  [muted]No system packages required.[/muted]")
            return
        system_deps.ensure_packages(packages, assume_yes=self.assume_yes)

    # -- Step 4: Environment setup -------------------------------------------
    def _step_environment(self) -> None:
        print_step(4, TOTAL_STEPS, "Environment Setup")

        # Re-discover tools that are installed but missing from this process's
        # PATH (common when launched from the GUI). Cheap, and saves a needless
        # reinstall of something the user already has.
        system_deps.refresh_path()

        # Many repos vendor dependencies as git submodules; a shallow clone won't
        # have them, and the project's own build (e.g. `make dev`) fails without
        # them. Initialise them up front when the repo declares any.
        self._init_submodules()

        # 4a. Prefer the project's OWN setup method if it ships one (make setup,
        #     setup.sh, task setup, just setup). It's the authoritative way to
        #     set the project up — we just ask before running repo-provided code.
        if self._try_project_setup():
            return  # the project's own setup ran (or fully handled this step)

        # 4b. Otherwise fall back to DevReady's language-native setup at the root.
        if self.detections:
            healer = self._make_healer(self.project_dir)
            for det in self.detections:
                console.print(f"  Setting up [bold]{det.language}[/bold]…")
                outcomes = version_manager.setup_environment(self.project_dir, det, healer)
                # Report any failed sub-steps so the user knows before we launch.
                # (The healer already streamed its diagnosis and retried; if it's
                # still failing here, auto-fixing wasn't possible.)
                for outcome in outcomes:
                    if not outcome.ok:
                        console.print(
                            f"  [warning]A setup command still failed after auto-fix attempts "
                            f"(exit {outcome.returncode}):\n"
                            f"  [muted]{outcome.command}[/muted]\n"
                            f"  See the diagnosis above. You can run the command manually for the "
                            f"full output.[/warning]"
                        )
                # Accumulate: once any language's install fails, the run is not
                # OK — a later language succeeding must not mask an earlier failure.
                if outcomes:
                    self._install_ok = self._install_ok and all(o.ok for o in outcomes)
        else:
            console.print("  [muted]No known stack at the project root.[/muted]")

        # 4c. Monorepos: set up sub-projects found one level down (e.g. a
        #     frontend/ Node app next to a Python backend).
        self._setup_subprojects()

    def _init_submodules(self) -> None:
        """Fetch git submodules when the repo declares them (``.gitmodules``).

        DevReady clones shallowly, so submodules aren't present; many projects'
        own build/run commands depend on them. Best-effort and quiet when absent.
        """
        if not (self.project_dir / ".gitmodules").exists():
            return
        console.print("  Fetching git submodules (this repo uses them)…")
        run_command(
            ["git", "submodule", "update", "--init", "--recursive"],
            cwd=str(self.project_dir),
            capture=False,
        )

    # Directories we never descend into when scanning a monorepo for sub-projects.
    _IGNORE_DIRS = {
        "node_modules", ".venv", "venv", "env", ".git", ".hg", "dist", "build",
        "__pycache__", ".devready", "target", "vendor", ".next", ".nuxt",
        ".idea", ".vscode", "site-packages", ".tox", ".pytest_cache", "bin", "obj",
    }

    def _setup_subprojects(self) -> None:
        """Detect and set up project components in immediate subdirectories.

        Each sub-project is set up with the same language-native logic as the
        root, in its own directory — so a monorepo (Python API + Node frontend,
        say) is fully bootstrapped. We ask before each one (unless --yes).
        """
        subprojects = self._detect_subprojects()
        if not subprojects:
            return

        console.print(f"  Found [bold]{len(subprojects)}[/bold] sub-project(s) inside this repo:")
        for subdir, results in subprojects:
            rel = subdir.relative_to(self.project_dir).as_posix()
            langs = ", ".join(r.language for r in results)
            if not self._confirm(f"    Set up [bold]{rel}[/bold] ({langs})? [Y/n] "):
                console.print(f"    [muted]Skipped {rel}.[/muted]")
                continue
            sub_healer = self._make_healer(subdir)
            for det in results:
                console.print(f"    Setting up {rel} ([bold]{det.language}[/bold])…")
                outcomes = version_manager.setup_environment(subdir, det, sub_healer)
                # A sub-project failure is reported but doesn't block the root
                # launch — the main app may still run fine.
                for outcome in outcomes:
                    if not outcome.ok:
                        console.print(
                            f"    [warning]{rel}: a setup command failed (exit {outcome.returncode}). "
                            f"Run it manually if you need this component.[/warning]"
                        )

    def _detect_subprojects(self):
        """Return [(subdir, detections)] for immediate subdirs that are projects."""
        found = []
        try:
            children = sorted(p for p in self.project_dir.iterdir() if p.is_dir())
        except OSError:
            return found
        for child in children:
            if child.name in self._IGNORE_DIRS or child.name.startswith("."):
                continue
            results = detect_stack(child)
            if results:
                found.append((child, results))
        return found

    def _try_project_setup(self) -> bool:
        """Offer to run the project's own setup method, if it has one.

        Returns True when a project-provided setup method handled this step
        (whether it was run or the user explicitly chose to use it). Returns
        False to fall through to DevReady's language-native setup — either
        because no method was found, the required tool is missing, or the user
        declined.
        """
        detected = strategies.detect_setup_strategies(self.project_dir)
        if not detected:
            return False

        strategy = detected[0]

        # If the tool that runs this setup isn't installed, offer to install it
        # and then continue — DevReady shouldn't dead-end on a missing tool.
        if not command_exists(strategy.runner):
            console.print(
                f"  This project sets up with [bold]{strategy.display}[/bold], "
                f"but [bold]{strategy.runner}[/bold] isn't installed."
            )
            if not self._confirm(
                f"  Install {strategy.runner} and run the project's setup? [Y/n] "
            ):
                console.print("  [muted]Skipping it — using DevReady's standard setup instead.[/muted]")
                return False
            if not system_deps.install_tool(strategy.runner):
                console.print("  [muted]Falling back to DevReady's standard setup.[/muted]")
                return False
            # Tool is now available — fall through and run the strategy.
        else:
            console.print(f"  This project provides its own setup: [bold]{strategy.display}[/bold]")
            if not self._confirm("  Run the project's setup instead of the default? [Y/n] "):
                console.print("  [muted]Skipping it — using DevReady's standard setup instead.[/muted]")
                return False

        console.print(f"  Running [bold]{strategy.display}[/bold]…")
        result = run_command(strategy.command, cwd=str(self.project_dir), capture=False)
        self._install_ok = result.ok
        self._project_setup_ran = result.ok
        if result.ok:
            console.print(f"  [success]Project setup completed via {strategy.display}.[/success]")
        else:
            console.print(
                f"  [error]{strategy.display} failed (exit {result.returncode}).[/error] "
                f"You can run it manually to see the full output."
            )
        return True

    # -- Step 5: Environment variables ---------------------------------------
    def _step_env_vars(self) -> None:
        print_step(5, TOTAL_STEPS, "Environment Variables")
        env_vars.generate_env_file(
            self.project_dir,
            readme_env_vars=self.insights.env_vars,
            interactive=not self.assume_yes,  # --yes leaves blanks rather than prompting
        )

    # -- Step 6: Docker / service orchestration ------------------------------
    def _step_docker(self) -> None:
        print_step(6, TOTAL_STEPS, "Services (Docker)")
        compose = self._find_compose_file()
        if compose is None:
            console.print("  [muted]No docker-compose file — skipping.[/muted]")
            return

        # This project ships a compose file, so Docker is a real dependency.
        # Treat it like any other: install it and start the engine if needed.
        if not system_deps.ensure_docker():
            return  # ensure_docker already explained what's needed

        if self._confirm(f"  Start services from {compose.name}? [Y/n] "):
            console.print("  Starting services (docker compose up -d)…")
            # `docker compose` (v2) is preferred; fall back to the hyphenated v1.
            base = ["docker", "compose"] if self._docker_compose_v2() else ["docker-compose"]
            result = run_command(base + ["up", "-d"], cwd=str(self.project_dir), capture=False)
            if result.ok:
                console.print("  [success]Services started.[/success]")
                self._write_state(docker=True)
            else:
                console.print("  [error]Failed to start services.[/error]")

    def _find_compose_file(self) -> Optional[Path]:
        for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
            candidate = self.project_dir / name
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def _docker_compose_v2() -> bool:
        """True if the modern ``docker compose`` subcommand is available."""
        return run_command(["docker", "compose", "version"]).ok

    # -- Step 7: Database migrations -----------------------------------------
    def _step_migrations(self) -> None:
        print_step(7, TOTAL_STEPS, "Database Migrations")

        # Prefer explicit db_commands the README/LLM gave us.
        if self.insights.db_commands:
            for cmd in self.insights.db_commands:
                console.print(f"  Running: [muted]{cmd}[/muted]")
                run_command(cmd, cwd=str(self.project_dir), shell=True, capture=False)
            return

        # Otherwise, auto-detect a common migration tool.
        py = version_manager.python_executable(self.project_dir)
        if (self.project_dir / "manage.py").exists() and py:
            console.print("  Detected Django — running migrate…")
            run_command([py, "manage.py", "migrate"], cwd=str(self.project_dir), capture=False)
        elif (self.project_dir / "alembic.ini").exists() and py:
            console.print("  Detected Alembic — running upgrade head…")
            run_command([py, "-m", "alembic", "upgrade", "head"], cwd=str(self.project_dir), capture=False)
        elif (self.project_dir / "knexfile.js").exists():
            console.print("  Detected Knex — running migrations…")
            run_command(["npx", "knex", "migrate:latest"], cwd=str(self.project_dir), capture=False)
        elif (self.project_dir / "bin" / "rails").exists() or (self.project_dir / "config" / "database.yml").exists():
            console.print("  Detected Rails — running db:migrate…")
            run_command(["bundle", "exec", "rails", "db:migrate"], cwd=str(self.project_dir), capture=False)
        elif (self.project_dir / "artisan").exists():
            console.print("  Detected Laravel — running migrate…")
            run_command(["php", "artisan", "migrate", "--force"], cwd=str(self.project_dir), capture=False)
        else:
            console.print("  [muted]No migration tool detected — skipping.[/muted]")

    # -- Step 8: Launch -------------------------------------------------------
    def _step_launch(self) -> None:
        print_step(8, TOTAL_STEPS, "Launch")

        # Don't launch if a dependency install step failed — the process would
        # crash immediately with a ModuleNotFoundError anyway, and the user
        # needs to fix dependencies first.
        if not self._install_ok:
            console.print(
                "  [warning]Skipping launch: one or more install steps failed.\n"
                "  Fix the dependency errors above, then re-run [bold]devready start[/bold].[/warning]"
            )
            return

        # Primary (mandatory when an LLM is configured): run the project the way
        # its README/guide documents. DevReady's framework heuristic is only the
        # fallback — the docs know the real entry point (e.g. `make dev`, not the
        # `npm run dev` that merely builds assets).
        guide = self._project_guide() if self.config.llm.is_configured else None
        if guide and self._has_runnable_web_command(guide):
            served = self._try_guided_launch(guide)
            if not served:
                self._render_guide(guide)  # documented command didn't serve — explain
            return

        targets = self._collect_launch_targets()
        served = self._launch_targets(targets) if targets else []
        if served:
            return  # a web app is up and the browser was opened — that's the finish

        # No reachable web URL: end with a clear, project-specific "how to use it"
        # guide instead of a bare "nothing to open".
        if guide:
            self._render_guide(guide)
        else:
            self._no_server_help()

    def _collect_launch_targets(self) -> List[dict]:
        """Find everything runnable: the root app plus any sub-project servers.

        Returns a list of ``{name, cwd, command, port}`` dicts. For a monorepo
        with, say, a backend and a frontend, this yields both so they start
        together. Components that aren't servers (no resolvable start command)
        are simply omitted.
        """
        targets: List[dict] = []

        cmd, port = self._resolve_launch()
        if cmd:
            targets.append({"name": "root", "cwd": str(self.project_dir), "command": cmd, "port": port})

        # Sub-project servers (e.g. a frontend/ that has an npm dev script).
        for subdir, results in self._detect_subprojects():
            sub = Engine(project_dir=subdir, config=self.config)
            sub.detections = results
            sub_cmd, sub_port = sub._resolve_launch()
            if sub_cmd:
                targets.append(
                    {"name": subdir.name, "cwd": str(subdir), "command": sub_cmd, "port": sub_port}
                )

        return targets

    def _launch_targets(self, targets: List[dict]) -> List[str]:
        """Spawn each target, wait for it, persist state, hand over URL(s).

        Returns the list of reachable web URLs (empty if nothing is serving), so
        the caller can decide whether to instead show a "how to use it" guide.
        """
        # Launch with the project's pinned Node on PATH (if any), so `npm run dev`
        # — and the pnpm/yarn its scripts invoke — use the right toolchain rather
        # than the system one (otherwise a Node-24 project crashes on Node 22).
        launch_env = self._launch_env()

        records: List[dict] = []
        for target in targets:
            self._attempted_commands.add(" ".join(target["command"]))
            record = self._spawn_and_check(target, env=launch_env)
            if record:
                records.append(record)

        if not records:
            return []  # crash details already shown

        # Persist all running processes (preserve any docker flag already set).
        self._write_state(processes=records, docker=self._read_state().get("docker", False))
        return self._announce_running(records)

    def _pinned_node_bin_dir(self) -> Optional[str]:
        """Return the fnm-managed Node bin dir if this project pins an unmet version."""
        node_det = next((d for d in self.detections if d.language == "Node.js"), None)
        if not node_det or not node_det.version:
            return None
        if version_manager._node_satisfies(node_det.version):
            return None  # the system Node already satisfies the pin
        return version_manager._fnm_node_bin_dir(node_det.version)

    def _launch_env(self) -> Optional[dict]:
        """Build the environment to launch in: the pinned Node's bin dir first on PATH.

        Returns None when the system Node is fine. The resolved bin dir is also
        persisted so ``devready run`` (which may have no fresh detections) can
        relaunch with the same toolchain.
        """
        env: Optional[dict] = None

        bin_dir = self._pinned_node_bin_dir()
        if not bin_dir:
            # Relaunch path: reuse what `start` persisted, if still valid.
            saved = self._read_state().get("node_bin_dir")
            bin_dir = saved if saved and Path(saved).exists() else None
        if bin_dir:
            env = os.environ.copy()
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
            env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"
            self._write_state(node_bin_dir=bin_dir)

        # If this project's npm scripts are shell scripts, launch `npm run …`
        # through bash too (same reasoning as install) so a Unix dev script runs
        # on Windows instead of crashing in cmd.exe.
        bash_shell = version_manager.needs_bash_script_shell(self.project_dir)
        if bash_shell:
            env = env or os.environ.copy()
            env["npm_config_script_shell"] = bash_shell

        return env

    def _spawn_and_check(self, target: dict, env: Optional[dict] = None) -> Optional[dict]:
        """Start one component, watch for an immediate crash, and verify its port.

        Output is streamed to a per-component log file (never a PIPE, which could
        deadlock a chatty long-running server). ``env`` lets the launch run with a
        pinned toolchain on PATH. Returns a state record on success, or None if it
        crashed (with the error shown).

        The record's ``port`` is set only if the server actually accepts a
        connection — so we never report (or persist) a URL that 404s/refuses. The
        guessed/announced port is kept in ``announced_port`` for an honest "still
        starting" message.
        """
        name, cwd, command, port = target["name"], target["cwd"], target["command"], target["port"]
        label = "" if name == "root" else f" [{name}]"
        console.print(f"  Launching{label}: [bold]{' '.join(command)}[/bold]")

        self._state_dir.mkdir(parents=True, exist_ok=True)
        log_name = "last-run.log" if name == "root" else f"last-run-{name}.log"
        log_path = self._state_dir / log_name
        try:
            log_file = open(log_path, "w", encoding="utf-8", errors="replace")
            # Resolve npm/npx/etc. to a launchable path on Windows (see utils),
            # searching the launch env's PATH so the pinned Node's npm is used.
            launch_cmd = _resolve_windows_executable(command, path=(env or {}).get("PATH"))
            process = subprocess.Popen(
                launch_cmd, cwd=cwd, stdout=log_file, stderr=subprocess.STDOUT, env=env
            )
        except (OSError, ValueError) as exc:
            console.print(f"  [error]Failed to launch{label}: {exc}[/error]")
            return None

        # Watch ~4s for an immediate crash (e.g. a wrong-version engine error).
        for _ in range(8):
            time.sleep(0.5)
            if process.poll() is not None:
                console.print(f"  [error]{name} exited immediately (code {process.returncode}).[/error]")
                self._print_log_tail(log_path)
                console.print(f"  [muted]Full log: {log_path}[/muted]")
                return None

        # Prefer the port the server actually announces in its log over our guess.
        announced = self._detect_port_from_log(log_path, port)
        reachable_port = announced if announced and self._wait_for_port(announced) else None
        return {
            "name": name,
            "pid": process.pid,
            "command": command,
            "port": reachable_port,        # only a port that truly responds
            "announced_port": announced,   # what we expect it on, for messaging
            "cwd": cwd,
        }

    # URLs/ports a dev server prints when it starts (vite, next, CRA, flask…).
    _LOG_URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{2,5})")
    _LOG_PORT_RE = re.compile(r"(?:port|listening on|running at|running on)\D{0,15}?(\d{2,5})", re.IGNORECASE)

    def _detect_port_from_log(self, log_path: Path, fallback: Optional[int]) -> Optional[int]:
        """Find the port a launched server announced in its log, else the fallback."""
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return fallback
        match = self._LOG_URL_RE.search(text) or self._LOG_PORT_RE.search(text)
        if match:
            value = int(match.group(1))
            if 1 <= value <= 65535:
                return value
        return fallback

    def _announce_running(self, records: List[dict]) -> List[str]:
        """Open the primary reachable URL and print an honest summary.

        Reachability was already determined in ``_spawn_and_check`` (``port`` is
        set only when the server truly responds). We never present a clickable URL
        that isn't actually up — instead we say it's still starting and show its
        recent output, so the user isn't sent to a dead localhost tab. Returns the
        list of reachable URLs that were announced.
        """
        opened = False
        served: List[str] = []
        console.print()
        for record in records:
            name = record["name"]
            port = record.get("port")  # reachable port, or None
            announced = record.get("announced_port")

            if port:
                url = f"http://localhost:{port}"
                served.append(url)
                console.print(f"  [success]✓ {name} → {url}[/success]")
                if not opened:
                    try:
                        webbrowser.open(url)
                        opened = True
                    except webbrowser.Error:
                        pass
            elif announced:
                # Process is alive but nothing is listening yet — be honest.
                console.print(
                    f"  [warning]• {name} started but isn't serving on port {announced} yet.[/warning]\n"
                    f"  [muted]It may still be building (some dev servers take a few minutes) or "
                    f"it serves on a different port. Recent output:[/muted]"
                )
                log_name = "last-run.log" if name == "root" else f"last-run-{name}.log"
                self._print_log_tail(self._state_dir / log_name, lines=12)
            else:
                # A CLI / worker with no web URL — alive is success.
                console.print(f"  [success]✓ {name} is running[/success] (no web URL).")

        if served:
            console.print("  Stop everything with [bold]devready stop[/bold].")
        return served

    def _resolve_launch(self) -> Tuple[Optional[List[str]], Optional[int]]:
        """Return ``(command, port)`` for starting the project, or ``(None, None)``.

        Picks a framework-appropriate start command and the port its web UI will
        listen on, so the launch step can open the right URL. The port from a
        project's ``.env`` always wins over our framework default.
        """
        is_python = any(d.language == "Python" for d in self.detections)
        frameworks = {f for d in self.detections for f in d.frameworks}
        py = version_manager.python_executable(self.project_dir) or "python"
        env_port = self._port_from_env()

        # Node: honour the conventional npm scripts.
        if (self.project_dir / "package.json").exists():
            try:
                scripts = json.loads(
                    (self.project_dir / "package.json").read_text(encoding="utf-8")
                ).get("scripts", {})
            except json.JSONDecodeError:
                scripts = {}
            for script in ("dev", "start", "serve"):
                if script in scripts:
                    return ["npm", "run", script], env_port or 3000

        if is_python:
            # Streamlit — this is the user-facing UI, so prefer it. Default 8501.
            if "Streamlit" in frameworks:
                entry = self._find_streamlit_entry()
                if entry:
                    p = env_port or 8501
                    return (
                        [py, "-m", "streamlit", "run", entry,
                         "--server.port", str(p), "--server.headless", "true"],
                        p,
                    )

            # Django — runserver on 8000.
            if (self.project_dir / "manage.py").exists():
                p = env_port or 8000
                return [py, "manage.py", "runserver", str(p)], p

            # FastAPI / Flask / generic: run the project's own entrypoint, which
            # typically starts its own server (uvicorn.run / app.run).
            entry = self._find_first(["main.py", "app.py", "run.py", "server.py"])
            if entry:
                default = 5000 if "Flask" in frameworks else 8000
                return [py, entry], env_port or default

        # Ruby — Rails serves on 3000; Sinatra defaults to 4567.
        if (self.project_dir / "Gemfile").exists():
            if "Rails" in frameworks or (self.project_dir / "bin" / "rails").exists():
                p = env_port or 3000
                return ["bundle", "exec", "rails", "server", "-p", str(p)], p
            entry = self._find_first(["app.rb", "main.rb", "server.rb"])
            if entry:
                return ["bundle", "exec", "ruby", entry], env_port or 4567

        # PHP — Laravel's artisan serve, else the built-in server on a docroot.
        if (self.project_dir / "composer.json").exists():
            if "Laravel" in frameworks or (self.project_dir / "artisan").exists():
                p = env_port or 8000
                return ["php", "artisan", "serve", f"--port={p}"], p
            docroot = "public" if (self.project_dir / "public").is_dir() else "."
            if (self.project_dir / "index.php").exists() or docroot == "public":
                p = env_port or 8000
                return ["php", "-S", f"localhost:{p}", "-t", docroot], p

        # Go — run the main package. Port is app-defined, so only claim a URL
        # when .env tells us one (the server otherwise prints its own).
        if (self.project_dir / "go.mod").exists():
            if (self.project_dir / "main.go").exists():
                return ["go", "run", "."], env_port

        # Rust — cargo run. Same port caveat as Go.
        if (self.project_dir / "Cargo.toml").exists():
            return ["cargo", "run"], env_port

        # Java — Spring Boot has a conventional run goal/task (port 8080).
        if "Spring Boot" in frameworks:
            if (self.project_dir / "pom.xml").exists():
                return [version_manager.maven_executable(self.project_dir), "spring-boot:run"], env_port or 8080
            return [version_manager.gradle_executable(self.project_dir), "bootRun"], env_port or 8080

        # .NET — `dotnet run`; ASP.NET defaults to 5000 unless launchSettings says otherwise.
        if any(d.language == ".NET" for d in self.detections):
            if "ASP.NET Core" in frameworks:
                return ["dotnet", "run"], env_port or self._dotnet_port() or 5000
            return ["dotnet", "run"], env_port

        return None, None

    def _dotnet_port(self) -> Optional[int]:
        """Read the HTTP port from a .NET project's launchSettings.json, if any."""
        for settings in self.project_dir.glob("**/launchSettings.json"):
            try:
                text = settings.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            # applicationUrl like "http://localhost:5165;https://localhost:7165"
            match = re.search(r"http://localhost:(\d+)", text)
            if match:
                return int(match.group(1))
            break  # only check the first one
        return None

    def _find_first(self, names: List[str]) -> Optional[str]:
        """Return the first of ``names`` that exists in the project, else None."""
        for name in names:
            if (self.project_dir / name).exists():
                return name
        return None

    def _no_server_help(self) -> None:
        """Guide the user when there's no web server to launch.

        Many projects are CLIs, libraries, or pipelines with no localhost URL.
        We make clear that *setup is done* and surface only ways to *run* the
        project — never setup/install commands (which would imply, misleadingly,
        that setup still needs doing).
        """
        # On the relaunch path detections may be empty; populate them so the
        # AI guide (and heuristics) have the project's stack as context.
        if not self.detections:
            self.detections = detect_stack(self.project_dir)

        targets = self._makefile_run_targets()
        run_commands = self._readme_run_commands()

        # Special case: a repo with no buildable stack whose README "run" commands
        # are actually tool *installers* (e.g. `winget install ...`, `irm ... | iex`,
        # `brew install --cask ...`). These projects aren't cloned-and-built — they're
        # installed via a package manager — so say that plainly instead of implying
        # a normal setup happened.
        if not self.detections and run_commands and self._looks_like_tool_installer(run_commands):
            console.print(
                "  [info]This repo is a tool you install via a package manager, not a "
                "project you clone and build.[/info]"
            )
            console.print("  To install it, run one of:")
            for cmd in run_commands[:5]:
                console.print(f"    [bold]{cmd}[/bold]")
            return

        # Primary path: a project-specific guide written by the LLM from the
        # README. If it identifies the documented web run-command, actually run
        # it (and open the browser); otherwise show the "how to use it" steps.
        # Falls through to the offline heuristics below when there's no LLM key.
        guide = self._project_guide()
        if guide:
            if self._try_guided_launch(guide):
                return  # the documented command served a URL — browser opened
            self._render_guide(guide)
            return

        if self._install_ok:
            # Setup succeeded — say so plainly so "no URL" doesn't read as failure.
            console.print(
                "  [success]Setup is complete.[/success] This is a CLI / library / pipeline "
                "project, so there's no web server or localhost URL to open."
            )
        else:
            console.print(
                "  [warning]No web-server entrypoint found, and setup didn't fully succeed — "
                "see the messages above.[/warning]"
            )

        if targets:
            console.print("  To run it, try:")
            for target in targets:
                console.print(f"    [bold]make {target}[/bold]")
        elif run_commands:
            console.print("  To run it, try:")
            for cmd in run_commands[:5]:
                console.print(f"    [muted]{cmd}[/muted]")
        else:
            console.print("  [muted]See the project's README for how to run it.[/muted]")

    def _project_guide(self) -> Optional[dict]:
        """Generate (once) the LLM "how to use this" guide dict, or None.

        Returns the structured guide ({what_it_is, has_web_ui, launch_command,
        url, steps, tips}) so callers can both *act* on it (run the documented
        command) and render it. None when there's no LLM key or no useful answer.
        """
        from .ai.guide import generate_project_guide

        readme = self._find_readme()
        readme_text = ""
        if readme is not None:
            try:
                readme_text = readme.read_text(encoding="utf-8", errors="replace")
            except OSError:
                readme_text = ""

        console.print("  [muted]Reading the project to work out how to run it…[/muted]")
        return generate_project_guide(
            self.config,
            self.project_dir,
            self.detections,
            self.insights,
            served_urls=[],
            readme_text=readme_text,
        )

    def _has_runnable_web_command(self, guide: dict) -> bool:
        """True if the guide gives a safe, documented command to start a web app."""
        from .ai.guide import is_safe_launch_command

        cmd = (guide.get("launch_command") or "").strip()
        return bool(guide.get("has_web_ui") and cmd and is_safe_launch_command(cmd))

    def _guide_needs_docker(self, guide: dict, cmd_str: str) -> bool:
        """Decide whether running this project requires Docker.

        From the documented command/steps/tips mentioning docker, or a compose
        file in the repo. DevReady then installs+starts Docker like any other dep.
        """
        blob = " ".join(
            [cmd_str, guide.get("tips", ""), " ".join(guide.get("steps") or [])]
        ).lower()
        if "docker" in blob:
            return True
        return self._find_compose_file() is not None

    def _try_guided_launch(self, guide: dict) -> List[str]:
        """If the guide names the documented web run-command, actually run it.

        DevReady's heuristic sometimes picks the wrong script (e.g. ``npm run dev``
        when the project's real entry is ``make dev``). When the README-derived
        guide says it's a web app and gives a safe single run-command we haven't
        already tried, run THAT and open the browser. Returns the served URLs.
        """
        from .ai.guide import is_safe_launch_command, port_from_url

        if not guide.get("has_web_ui"):
            return []
        cmd_str = (guide.get("launch_command") or "").strip()
        if not cmd_str or not is_safe_launch_command(cmd_str):
            return []
        if cmd_str in self._attempted_commands:
            return []  # don't re-run the same command the heuristic already tried

        from .environment import system_deps

        # If running this needs Docker, make it available first (install + start),
        # treating Docker as a normal dependency.
        if self._guide_needs_docker(guide, cmd_str):
            if not system_deps.ensure_docker():
                console.print(
                    "  [warning]This project needs Docker to run — see the steps below.[/warning]"
                )
                return []

        # Make sure the runner exists. make/just/task are cheap to auto-install;
        # others (docker, etc.) we leave — the launch will report honestly if absent.
        head = cmd_str.split()[0].lower()
        if not command_exists(head) and head in ("make", "just", "task"):
            console.print(f"  Installing [bold]{head}[/bold] (needed to run this project)…")
            system_deps.install_tool(head)

        port = port_from_url(guide.get("url", ""))
        console.print(
            f"\n  The project's documented way to run is [bold]{cmd_str}[/bold] — starting it for you…"
        )
        target = {
            "name": "root",
            "cwd": str(self.project_dir),
            "command": cmd_str.split(),
            "port": port,
        }
        return self._launch_targets([target])

    def _render_guide(self, guide: dict) -> None:
        """Print the project guide dict produced by :meth:`_project_guide`."""
        print_banner("[bold cyan]How to use this project[/bold cyan] 📖")
        what = guide.get("what_it_is", "")
        if what:
            console.print(f"  {what}\n")
        steps = guide.get("steps") or []
        if steps:
            console.print("  [bold]Steps to run / use it:[/bold]")
            for i, step in enumerate(steps, 1):
                console.print(f"    [bold]{i}.[/bold] {step}")
        tips = guide.get("tips", "")
        if tips:
            console.print(f"\n  [muted]Note: {tips}[/muted]")
        if not guide.get("has_web_ui"):
            console.print(
                "\n  [muted]This project doesn't open as a website — follow the steps above "
                "to use it.[/muted]"
            )

    # Command fragments that indicate "install this tool via a package manager"
    # rather than "run this cloned project" — used to recognise repos that aren't
    # meant to be cloned and built (e.g. the claude-code CLI).
    _TOOL_INSTALLER_PATTERNS = (
        "winget install", "brew install --cask", "scoop install", "choco install",
        "| iex", "irm ", "iwr ", "| sh", "| bash", "snap install", "apt install",
        "apt-get install", "npm install -g", "npm i -g", "pipx install",
    )

    def _looks_like_tool_installer(self, commands: List[str]) -> bool:
        """True if the given commands are predominantly tool-installer invocations."""
        return any(
            any(pat in cmd.lower() for pat in self._TOOL_INSTALLER_PATTERNS)
            for cmd in commands
        )

    # Commands that are about *setting up* rather than *running* — we never show
    # these as "how to run it" (they'd imply setup isn't finished).
    _SETUP_NOISE = (
        "git clone", "cd ", "pip install", "pip3 install", "npm install", "npm ci",
        "yarn", "pnpm install", "poetry install", "pipenv install", "bundle install",
        "composer install", "cargo build", "go mod", "dotnet restore", "make setup",
        "make install", "make bootstrap", "make init", "setup.sh", "install.sh",
        "bootstrap.sh", "cp .env", "python -m venv", "virtualenv", "./configure",
        "pip install -r",
    )

    def _readme_run_commands(self) -> List[str]:
        """README commands that look like ways to *run* the project, not set it up."""
        run_cmds: List[str] = []
        for cmd in self.insights.commands:
            if cmd and not any(noise in cmd.lower() for noise in self._SETUP_NOISE):
                run_cmds.append(cmd)
        return run_cmds

    def _makefile_run_targets(self) -> List[str]:
        """Return Makefile targets that look like ways to run/demo the project."""
        makefile = self.project_dir / "Makefile"
        if not makefile.exists():
            return []
        try:
            text = makefile.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []
        targets = re.findall(r"^([a-zA-Z0-9_-]+):", text, re.MULTILINE)
        known = {"run", "start", "serve", "dev", "demo", "up", "dev-server", "runserver"}
        return [
            t for t in targets
            if t in known or t.startswith(("run", "start", "serve", "demo"))
        ]

    def _find_streamlit_entry(self) -> Optional[str]:
        """Find the file that is actually the Streamlit app.

        Filename alone is unreliable: a project can have a root ``Main.py`` that
        is a FastAPI backend AND a ``webui/Main.py`` that is the Streamlit UI
        (e.g. MoneyPrinterTurbo). Running the wrong one gives a blank page. So we
        pick the first candidate whose source actually imports streamlit, and
        only fall back to a filename guess if none can be confirmed.
        """
        candidates = [
            "streamlit_app.py", "app.py", "Main.py", "main.py",
            "webui/Main.py", "webui/app.py", "src/Main.py", "src/app.py",
            "ui/app.py", "frontend/app.py",
        ]

        # 1. Prefer a known candidate that genuinely uses Streamlit.
        for name in candidates:
            path = self.project_dir / name
            if path.exists() and self._uses_streamlit(path):
                return name

        # 2. Otherwise scan the top two directory levels for any Streamlit file.
        for pattern in ("*.py", "*/*.py"):
            for path in sorted(self.project_dir.glob(pattern)):
                if self._uses_streamlit(path):
                    return path.relative_to(self.project_dir).as_posix()

        # 3. Last resort: first existing candidate, even if unconfirmed.
        return self._find_first(candidates)

    @staticmethod
    def _uses_streamlit(path: Path) -> bool:
        """Return True if a Python file imports Streamlit."""
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False
        return "import streamlit" in text or "streamlit as st" in text

    def _port_from_env(self) -> Optional[int]:
        """Read a PORT value from the project's .env, if present."""
        env_file = self.project_dir / ".env"
        if env_file.exists():
            match = re.search(r"^PORT=(\d+)", env_file.read_text(encoding="utf-8"), re.MULTILINE)
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def _wait_for_port(port: int, timeout: int = 25) -> bool:
        """Block until ``port`` accepts a TCP connection, or ``timeout`` elapses."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    return True
            time.sleep(0.5)
        return False

    @staticmethod
    def _print_log_tail(log_path: Path, lines: int = 8) -> None:
        """Print the last few lines of the server log to help diagnose a crash."""
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return
        if content:
            tail = "\n".join(content.splitlines()[-lines:])
            console.print(f"  [muted]{tail}[/muted]")

    # =========================================================================
    # Public command: status
    # =========================================================================
    def status(self) -> None:
        """Report whether the project's server(s) and services are running."""
        state = self._read_state()
        processes = self._state_processes(state)

        table = Table(title="DevReady status", show_header=False)
        table.add_column("Field", style="bold")
        table.add_column("Value")
        table.add_row("Project", str(self.project_dir))

        any_running = False
        if processes:
            for proc in processes:
                pid = proc.get("pid")
                running = bool(pid and _pid_alive(pid))
                any_running = any_running or running
                state_text = f"[success]running[/success] (pid {pid})" if running else "[muted]not running[/muted]"
                port = proc.get("port")
                url = f" — http://localhost:{port}" if port else ""
                table.add_row(proc.get("name", "server"), state_text + url)
        else:
            table.add_row("Server", "[muted]nothing set up yet[/muted]")

        table.add_row("Docker services", "started" if state.get("docker") else "—")
        console.print(table)

        if not any_running and processes:
            console.print("\n[muted]Run [bold]devready run[/bold] to relaunch.[/muted]")

    # =========================================================================
    # Public command: stop
    # =========================================================================
    def stop(self) -> None:
        """Stop every launched component and any Docker services we started."""
        state = self._read_state()
        processes = self._state_processes(state)

        stopped = 0
        for proc in processes:
            pid = proc.get("pid")
            if pid and _pid_alive(pid):
                console.print(f"  Stopping {proc.get('name', 'server')} (pid {pid})…")
                _terminate_pid(pid)
                stopped += 1
        if stopped:
            console.print(f"  [success]Stopped {stopped} process(es).[/success]")
        else:
            console.print("  [muted]No running processes recorded.[/muted]")

        if state.get("docker") and command_exists("docker"):
            console.print("  Stopping Docker services…")
            base = ["docker", "compose"] if self._docker_compose_v2() else ["docker-compose"]
            run_command(base + ["down"], cwd=str(self.project_dir), capture=False)

        # Clear the runtime fields but keep the state file around.
        self._write_state(processes=[], pid=None, docker=False)

    # =========================================================================
    # Public command: clean
    # =========================================================================
    def clean(self) -> None:
        """Remove DevReady-managed artifacts (.venv, .devready state).

        We deliberately do NOT touch the user's source code, .env, or
        node_modules unless asked — clean is about undoing what DevReady set up.
        """
        # Make sure nothing is still running before we delete its state.
        self.stop()

        import shutil

        targets = [self.project_dir / ".venv", self._state_dir]
        for target in targets:
            if target.exists():
                console.print(f"  Removing {target.name}…")
                shutil.rmtree(target, ignore_errors=True)
        console.print("  [success]Clean complete.[/success]")

    # =========================================================================
    # Smart preflight: what does this project need vs. what's installed?
    # =========================================================================
    @staticmethod
    def _req(name: str, needs: str, have: str, ready: bool, action: str) -> dict:
        """Build one requirement row for the preflight report."""
        return {"name": name, "needs": needs, "have": have, "ready": ready, "action": action}

    def requirements_report(self) -> List[dict]:
        """Analyse what this project needs vs. what's installed, before installing.

        Returns a list of requirement rows ({name, needs, have, ready, action}).
        ``ready`` means it's already satisfied; otherwise ``action`` describes
        what DevReady will do (install/provision it). This powers the "plan"
        shown during ``start`` and ``devready doctor <path>``.
        """
        from .environment import version_manager as vm

        detections = self.detections or detect_stack(self.project_dir)
        items: List[dict] = []

        for det in detections:
            lang = det.language
            if lang == "Node.js":
                have = vm._node_version()
                if det.version:
                    ok = vm._node_satisfies(det.version)
                    items.append(self._req(
                        "Node.js", f">= {det.version}", have or "not installed",
                        ok, f"install Node {det.version} (via fnm)"))
                else:
                    items.append(self._req(
                        "Node.js", "any recent", have or "not installed",
                        bool(have), "install Node (via your package manager)"))
                pm = vm._node_package_manager(self.project_dir)
                if pm == "npm":
                    items.append(self._req("npm", "package manager",
                        "yes" if command_exists("npm") else "no",
                        command_exists("npm"), "comes with Node"))
                else:
                    ok_pm = command_exists(pm) or command_exists("corepack")
                    items.append(self._req(pm, "package manager",
                        "yes" if command_exists(pm) else ("via corepack" if command_exists("corepack") else "no"),
                        ok_pm, f"provision {pm} (via corepack)"))

            elif lang == "Python":
                cur = vm._interpreter_version(sys.executable)
                have = f"{cur[0]}.{cur[1]}" if cur else "not found"
                if det.version:
                    found = vm.find_installed_python(det.version) is not None
                    items.append(self._req(
                        "Python", det.version, have if found else f"{have} (mismatch)",
                        found, f"download Python {det.version} (via uv)"))
                else:
                    items.append(self._req("Python", "any 3.x", have, bool(cur), "use current Python"))
                items.append(self._req(
                    "Isolated env (.venv)", "per-project", "—", False, "create .venv + install deps"))

            else:
                runner = {
                    "Rust": "cargo", "Go": "go", "Ruby": "bundle", "PHP": "composer",
                    ".NET": "dotnet", "Java": "mvn",
                }.get(lang, lang.lower())
                have = command_exists(runner)
                items.append(self._req(
                    lang, runner, "yes" if have else "no", have, f"install {runner}"))

        # The project's own setup runner (make/just/task), if it ships one.
        try:
            strat = strategies.detect_setup_strategies(self.project_dir)
            for s in strat[:1]:  # the one DevReady would use
                have = command_exists(s.runner)
                items.append(self._req(
                    f"{s.runner} (project setup)", s.display, "yes" if have else "no",
                    have, f"install {s.runner}"))
        except Exception:
            pass  # strategy detection is best-effort; never block the report

        # System packages the README mentions (ffmpeg, postgres…). Cleaned and
        # de-duplicated; language runtimes are dropped (handled above).
        if self.insights and self.insights.system_packages:
            to_install, _ = system_deps.normalize_packages(self.insights.system_packages)
            for pkg in to_install:
                have = system_deps.is_installed(pkg)
                items.append(self._req(
                    pkg, "system package", "yes" if have else "no",
                    have, f"install {pkg} (via your package manager)"))

        # Environment file: if the README declares env vars or the repo ships an
        # example, DevReady will generate a .env with safe local defaults.
        env_count = len(self.insights.env_vars) if self.insights else 0
        has_example = any(
            (self.project_dir / name).exists() for name in (".env.example", ".env.sample", ".env.template")
        )
        env_exists = (self.project_dir / ".env").exists()
        if env_count or has_example:
            needs = f"{env_count} variable(s)" if env_count else "from example"
            items.append(self._req(
                ".env file", needs, "present" if env_exists else "—",
                env_exists, "generate .env with safe dev defaults"))

        return items

    def _print_plan(self) -> None:
        """Print the complete preflight plan (called after README analysis)."""
        report = self.requirements_report()
        if report:
            self._print_requirements(report)

    def _print_requirements(self, items: List[dict]) -> None:
        """Render the preflight plan: what's ready, and what DevReady will set up."""
        table = Table(title="Plan — what this project needs", show_header=True, header_style="bold")
        table.add_column("Component")
        table.add_column("Needs")
        table.add_column("You have")
        table.add_column("Plan")
        for it in items:
            plan = "[success]✓ ready[/success]" if it["ready"] else f"[info]⬇ {it['action']}[/info]"
            table.add_row(it["name"], it["needs"], it["have"], plan)
        console.print(table)
        pending = [it for it in items if not it["ready"]]
        if pending:
            console.print(f"  [muted]DevReady will set up {len(pending)} item(s) for you automatically.[/muted]")

    # =========================================================================
    # Public command: doctor
    # =========================================================================
    def doctor(self) -> None:
        """Print a diagnostic report of the local toolchain and config.

        This is the first thing to run when something goes wrong: it shows which
        tools DevReady can see and whether the LLM is configured. When run inside
        a project, it also shows that project's requirement plan.
        """
        print_banner("DevReady doctor 🩺")

        table = Table(show_header=True, header_style="bold")
        table.add_column("Check")
        table.add_column("Status")

        # Toolchain availability.
        tools = (
            "python", "pip", "uv",            # Python toolchain (uv = version manager)
            "node", "npm", "fnm",             # Node toolchain
            "cargo", "go", "ruby", "bundle", "php", "composer",  # other ecosystems
            "docker", "make", "git",          # services / build / vcs
        )
        for tool in tools:
            present = command_exists(tool)
            table.add_row(tool, "[success]found[/success]" if present else "[muted]missing[/muted]")

        # LLM configuration.
        if self.config.llm.is_configured:
            table.add_row("LLM", f"[success]configured[/success] ({self.config.llm.model})")
        else:
            table.add_row("LLM", "[warning]not configured — using regex fallback[/warning]")

        console.print(table)

        # If we're inside a recognised project, show its requirement plan too —
        # what it needs vs. what's installed, before you run `devready start`.
        # Use the fast offline README parser here so doctor stays quick/offline.
        readme = self._find_readme()
        if readme is not None and self.insights.is_empty:
            from .ai.readme_parser import _parse_with_regex

            try:
                self.insights = _parse_with_regex(readme.read_text(encoding="utf-8"))
            except OSError:
                pass
        report = self.requirements_report()
        if report:
            console.print(f"\n[muted]Project at {self.project_dir}:[/muted]")
            self._print_requirements(report)
        else:
            console.print(
                "\n[muted]Run inside a project (or `devready doctor <path>`) to see its "
                "requirement plan.[/muted]"
            )

    # =========================================================================
    # Public command: list (all projects DevReady has set up)
    # =========================================================================
    @classmethod
    def list_all(cls) -> None:
        """Print every project DevReady has set up, with its current run status."""
        projects = list_projects()
        if not projects:
            console.print(
                "[muted]No projects yet. Run [bold]devready start[/bold] in a project "
                "to get going.[/muted]"
            )
            return

        table = Table(title="DevReady projects", show_header=True, header_style="bold")
        table.add_column("Project")
        table.add_column("Status")
        table.add_column("URL(s)")

        for entry in projects:
            path = Path(entry.get("path", ""))
            if not path.exists():
                table.add_row(str(path), "[muted]missing[/muted]", "—")
                continue

            engine = cls(project_dir=path)
            processes = engine._state_processes(engine._read_state())
            running = [p for p in processes if p.get("pid") and _pid_alive(p["pid"])]

            if running:
                status = "[success]running[/success]"
                ports = [p["port"] for p in running if p.get("port")]
            else:
                status = "[muted]stopped[/muted]"
                ports = [p["port"] for p in processes if p.get("port")]
            urls = ", ".join(f"localhost:{port}" for port in ports) or "—"
            table.add_row(str(path), status, urls)

        console.print(table)
        console.print("\n[muted]cd into any of these and run [bold]devready run[/bold] to relaunch.[/muted]")


# -----------------------------------------------------------------------------
# Cross-platform process helpers (module-level so they're easy to unit test)
# -----------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` is currently running."""
    if pid is None:
        return False
    try:
        if os.name == "nt":
            # On Windows, query the task list for the PID.
            result = run_command(["tasklist", "/FI", f"PID eq {pid}"])
            return str(pid) in result.stdout
        # On POSIX, signal 0 checks existence without actually signalling.
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _terminate_pid(pid: int) -> None:
    """Terminate a process by PID, using the right mechanism per OS."""
    try:
        if os.name == "nt":
            # /T also kills child processes (e.g. npm -> node).
            run_command(["taskkill", "/PID", str(pid), "/T", "/F"])
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
