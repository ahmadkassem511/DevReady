"""Install jobs — clone a repo and run the setup pipeline, streaming the log.

When the user clicks **Install** in the GUI, the browser can't (and shouldn't)
run anything itself. Instead the local server kicks off a *job* here: a
background thread that clones the repo and then runs ``devready start --yes`` as
a subprocess, pushing every line of output onto a queue. The web layer drains
that queue and streams it to the browser as Server-Sent Events, so the user
watches real progress in a log panel — no terminal, identical on every OS.

Running setup as a subprocess (rather than calling :class:`Engine` in-process)
keeps the long pipeline off the web server's event loop, isolates crashes, and
reuses the exact same code path the CLI uses.

Security notes:
  * Repo URLs are validated against an allowlist of hosts and must be https —
    we never run an arbitrary string, and the URL is passed to ``git`` as an
    argv element (no shell), so it can't inject commands.
  * Clones land in a single workspace directory under the user's home.
"""

from __future__ import annotations

import queue
import re
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

# Git hosts we allow cloning from. Keeps the install surface to reputable,
# well-known sources rather than "anything that parses as a URL".
ALLOWED_GIT_HOSTS = {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"}

# A sentinel pushed onto a job's queue to mark the end of the log stream.
_DONE = object()


def workspace_dir() -> Path:
    """Return the directory cloned projects live in (``~/DevReadyProjects``)."""
    return Path.home() / "DevReadyProjects"


def validate_repo_url(repo_url: str) -> str:
    """Validate and normalise a clone URL, or raise ``ValueError``.

    Enforces https + an allowlisted host so the GUI can only ever clone from
    reputable sources. Returns the cleaned URL.
    """
    url = (repo_url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ValueError("Repository URL must start with https://")
    if parsed.hostname not in ALLOWED_GIT_HOSTS:
        allowed = ", ".join(sorted(ALLOWED_GIT_HOSTS))
        raise ValueError(f"Only these git hosts are allowed: {allowed}")
    return url.rstrip("/")


def project_name_from_url(repo_url: str) -> str:
    """Derive a safe local folder name from a repo URL (the repo's name)."""
    tail = urlparse(repo_url).path.rstrip("/").split("/")[-1]
    name = re.sub(r"\.git$", "", tail)
    # Defensive: strip anything that isn't a sane folder character.
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name) or "project"
    return name


@dataclass
class Job:
    """One install job: its log queue, status, and resulting project info."""

    id: str
    repo_url: str
    name: str
    status: str = "running"  # running | success | error
    project_dir: Optional[str] = None
    urls: List[str] = field(default_factory=list)
    queue: "queue.Queue" = field(default_factory=queue.Queue)


class JobManager:
    """Creates and tracks install jobs. One instance lives on the app."""

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def start_install(self, repo_url: str) -> Job:
        """Validate the URL, create a job, and run the install in a thread."""
        clean_url = validate_repo_url(repo_url)
        job = Job(id=uuid.uuid4().hex, repo_url=clean_url, name=project_name_from_url(clean_url))
        self._jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    # -- internals -----------------------------------------------------------
    def _emit(self, job: Job, line: str) -> None:
        """Push one log line to the job's queue (drained by the SSE stream)."""
        job.queue.put(line.rstrip("\n"))

    def _run(self, job: Job) -> None:
        """Clone then set up the project, streaming output to the job queue."""
        try:
            workspace = workspace_dir()
            workspace.mkdir(parents=True, exist_ok=True)
            target = workspace / job.name

            if target.exists():
                self._emit(job, f"→ Project already cloned at {target}; reusing it.")
            else:
                self._emit(job, f"→ Cloning {job.repo_url} …")
                code = self._stream(
                    ["git", "clone", "--depth", "1", job.repo_url, str(target)], job
                )
                if code != 0:
                    job.status = "error"
                    self._emit(job, "✗ Clone failed. Check the URL and your connection.")
                    return

            job.project_dir = str(target)
            self._emit(job, "")
            self._emit(job, "→ Setting up the project (this can take a few minutes) …")

            # Reuse the exact CLI path: `python -m devready start <dir> --yes`.
            code = self._stream(
                [sys.executable, "-m", "devready", "start", str(target), "--yes"], job
            )

            if code == 0:
                job.status = "success"
                job.urls = self._read_urls(target)
            else:
                job.status = "error"
        except Exception as exc:  # never let a job thread die silently
            job.status = "error"
            self._emit(job, f"✗ Unexpected error: {exc}")
        finally:
            job.queue.put(_DONE)

    def _stream(self, command: List[str], job: Job) -> int:
        """Run ``command``, emitting each output line; return the exit code."""
        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            self._emit(job, f"✗ Command not found: {command[0]}")
            return 127

        assert proc.stdout is not None
        for line in proc.stdout:
            self._emit(job, line)
        proc.wait()
        return proc.returncode

    def _read_urls(self, project_dir: Path) -> List[str]:
        """After setup, read the project's saved state for any launched URLs."""
        from ..engine import Engine  # local import to avoid a heavy import cycle

        engine = Engine(project_dir=project_dir)
        processes = engine._state_processes(engine._read_state())
        return [
            f"http://localhost:{p['port']}" for p in processes if p.get("port")
        ]
