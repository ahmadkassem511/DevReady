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
import time
import webbrowser
from pathlib import Path
from typing import List, Optional

from rich.table import Table

from .ai import ReadmeInsights, parse_readme
from .config import Config
from .detectors import DetectionResult, detect_stack
from .environment import env_vars, system_deps, version_manager
from .utils import (
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

    def __init__(self, project_dir: Optional[Path] = None, config: Optional[Config] = None):
        # Default to the current working directory; resolve to an absolute path
        # so state files and subprocesses behave predictably.
        self.project_dir = (project_dir or Path.cwd()).resolve()
        self.config = config or Config.load()

        # Populated as the pipeline runs; later steps read these.
        self.detections: List[DetectionResult] = []
        self.insights: ReadmeInsights = ReadmeInsights()
        self._install_ok: bool = True  # set False if a dep-install step fails

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
    def start(self) -> None:
        """Run the complete setup pipeline, stopping early only on fatal errors."""
        print_banner("[bold cyan]DevReady[/bold cyan] — getting your project ready 🚀")
        console.print(f"[muted]Project: {self.project_dir}[/muted]")

        self._step_detect()
        self._step_analyze_readme()
        self._step_system_deps()
        self._step_environment()
        self._step_env_vars()
        self._step_docker()
        self._step_migrations()
        self._step_launch()

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
        system_deps.ensure_packages(packages)

    # -- Step 4: Environment setup -------------------------------------------
    def _step_environment(self) -> None:
        print_step(4, TOTAL_STEPS, "Environment Setup")
        if not self.detections:
            console.print("  [muted]No known stack to set up.[/muted]")
            return
        for det in self.detections:
            console.print(f"  Setting up [bold]{det.language}[/bold]…")
            outcomes = version_manager.setup_environment(self.project_dir, det)
            # Report any failed sub-steps so the user knows before we try to launch.
            for outcome in outcomes:
                if not outcome.ok:
                    console.print(
                        f"  [warning]A setup command exited with code {outcome.returncode}:\n"
                        f"  [muted]{outcome.command}[/muted]\n"
                        f"  Some dependencies may be missing. Run the command above manually "
                        f"to see the full error.[/warning]"
                    )
            self._install_ok = all(o.ok for o in outcomes) if outcomes else True

    # -- Step 5: Environment variables ---------------------------------------
    def _step_env_vars(self) -> None:
        print_step(5, TOTAL_STEPS, "Environment Variables")
        env_vars.generate_env_file(
            self.project_dir,
            readme_env_vars=self.insights.env_vars,
            interactive=True,
        )

    # -- Step 6: Docker / service orchestration ------------------------------
    def _step_docker(self) -> None:
        print_step(6, TOTAL_STEPS, "Services (Docker)")
        compose = self._find_compose_file()
        if compose is None:
            console.print("  [muted]No docker-compose file — skipping.[/muted]")
            return
        if not command_exists("docker"):
            console.print("  [warning]docker-compose found but Docker isn't installed.[/warning]")
            return

        # `docker` on PATH doesn't mean the daemon is running. `docker info`
        # talks to the daemon and fails fast if it's not started yet.
        daemon_check = run_command(["docker", "info"])
        if not daemon_check.ok:
            console.print(
                "  [warning]Docker is installed but the daemon isn't running.\n"
                "  Start Docker Desktop (or run 'sudo systemctl start docker' on Linux),\n"
                "  then re-run [bold]devready start[/bold] to bring up services.[/warning]"
            )
            return

        answer = console.input(f"  Start services from {compose.name}? [Y/n] ").strip().lower()
        if answer in ("", "y", "yes"):
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

        start_cmd = self._determine_start_command()
        if start_cmd is None:
            console.print("  [muted]Couldn't determine a start command. Set it up manually.[/muted]")
            return

        console.print(f"  Launching: [bold]{' '.join(start_cmd)}[/bold]")
        try:
            # Capture stderr so we can show it if the process crashes on startup.
            process = subprocess.Popen(
                start_cmd,
                cwd=str(self.project_dir),
                stderr=subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            console.print(f"  [error]Failed to launch: {exc}[/error]")
            return

        # Poll for 3 seconds: if the process exits immediately it crashed.
        for _ in range(6):
            time.sleep(0.5)
            if process.poll() is not None:
                stderr_out = ""
                try:
                    stderr_out = (process.stderr.read() or b"").decode(errors="replace").strip()
                except Exception:
                    pass
                console.print(f"  [error]Server exited immediately (code {process.returncode}).[/error]")
                if stderr_out:
                    # Show just the last few lines — enough to diagnose the problem.
                    last_lines = "\n".join(stderr_out.splitlines()[-6:])
                    console.print(f"  [muted]{last_lines}[/muted]")
                console.print(
                    "  [warning]Tip: activate the venv manually and run the command above\n"
                    "  to see the full traceback:[/warning]\n"
                    f"  [bold]{' '.join(start_cmd)}[/bold]"
                )
                return
            break  # still running after first half-second — looks good

        self._write_state(pid=process.pid, start_command=start_cmd)

        # Give the server a moment to bind its port, then open the browser.
        port = self._guess_port()
        if port:
            url = f"http://localhost:{port}"
            console.print(f"  Opening [link={url}]{url}[/link] …")
            time.sleep(2)
            try:
                webbrowser.open(url)
            except webbrowser.Error:
                pass

        console.print(
            "  [success]Project is running.[/success] "
            "Use [bold]devready stop[/bold] to shut it down."
        )

    def _determine_start_command(self) -> Optional[List[str]]:
        """Work out how to start the app from package.json scripts or framework hints."""
        # Node: honour the conventional "start" / "dev" npm scripts.
        package_json = self.project_dir / "package.json"
        if package_json.exists():
            try:
                scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts", {})
            except json.JSONDecodeError:
                scripts = {}
            for script in ("dev", "start", "serve"):
                if script in scripts:
                    return ["npm", "run", script]

        # Python frameworks.
        py = version_manager.python_executable(self.project_dir) or "python"
        if (self.project_dir / "manage.py").exists():
            return [py, "manage.py", "runserver"]
        # FastAPI/Flask via a conventional app entry point.
        if any(d.language == "Python" for d in self.detections):
            if (self.project_dir / "main.py").exists():
                return [py, "main.py"]
            if (self.project_dir / "app.py").exists():
                return [py, "app.py"]

        return None

    def _guess_port(self) -> Optional[int]:
        """Guess the dev server port from .env or fall back to common defaults."""
        env_file = self.project_dir / ".env"
        if env_file.exists():
            match = re.search(r"^PORT=(\d+)", env_file.read_text(encoding="utf-8"), re.MULTILINE)
            if match:
                return int(match.group(1))
        # Sensible defaults: Django/Flask 8000, Node 3000.
        if (self.project_dir / "manage.py").exists():
            return 8000
        if (self.project_dir / "package.json").exists():
            return 3000
        return None

    # =========================================================================
    # Public command: status
    # =========================================================================
    def status(self) -> None:
        """Report whether the project's server and services are running."""
        state = self._read_state()
        pid = state.get("pid")

        table = Table(title="DevReady status", show_header=False)
        table.add_column("Field", style="bold")
        table.add_column("Value")
        table.add_row("Project", str(self.project_dir))

        if pid and _pid_alive(pid):
            table.add_row("Server", f"[success]running[/success] (pid {pid})")
            table.add_row("Command", " ".join(state.get("start_command", [])))
        else:
            table.add_row("Server", "[muted]not running[/muted]")

        table.add_row("Docker services", "started" if state.get("docker") else "—")
        console.print(table)

    # =========================================================================
    # Public command: stop
    # =========================================================================
    def stop(self) -> None:
        """Stop the launched server and any Docker services we started."""
        state = self._read_state()
        pid = state.get("pid")

        if pid and _pid_alive(pid):
            console.print(f"  Stopping server (pid {pid})…")
            _terminate_pid(pid)
            console.print("  [success]Server stopped.[/success]")
        else:
            console.print("  [muted]No running server recorded.[/muted]")

        if state.get("docker") and command_exists("docker"):
            console.print("  Stopping Docker services…")
            base = ["docker", "compose"] if self._docker_compose_v2() else ["docker-compose"]
            run_command(base + ["down"], cwd=str(self.project_dir), capture=False)

        # Clear the runtime fields but keep the state file around.
        self._write_state(pid=None, docker=False)

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
    # Public command: doctor
    # =========================================================================
    def doctor(self) -> None:
        """Print a diagnostic report of the local toolchain and config.

        This is the first thing to run when something goes wrong: it shows which
        tools DevReady can see and whether the LLM is configured.
        """
        print_banner("DevReady doctor 🩺")

        table = Table(show_header=True, header_style="bold")
        table.add_column("Check")
        table.add_column("Status")

        # Toolchain availability.
        for tool in ("python", "pip", "node", "npm", "docker", "git", "pyenv", "nvm"):
            present = command_exists(tool)
            table.add_row(tool, "[success]found[/success]" if present else "[muted]missing[/muted]")

        # LLM configuration.
        if self.config.llm.is_configured:
            table.add_row("LLM", f"[success]configured[/success] ({self.config.llm.model})")
        else:
            table.add_row("LLM", "[warning]not configured — using regex fallback[/warning]")

        console.print(table)


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
