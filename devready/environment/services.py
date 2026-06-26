"""Auto-provision backing services (Postgres, Redis, MySQL, Mongo) a repo needs.

Many apps don't ship a ``docker-compose`` file but still need a database or cache
to *run* — they simply expect one at ``localhost:5432`` (etc.). When DevReady
detects such a dependency, it starts a standard container for it through whatever
container engine is available (Docker or Podman, via the same ``docker`` command
— Podman provides a shim), using credentials that match the dev defaults
DevReady writes into ``.env``.

Idempotent and safe:
  * If something is already listening on the service's port, we use it.
  * If DevReady previously created the container, we just restart it.
  * Containers are named ``devready-<service>`` so ``devready stop`` can stop them.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from ..utils import console, run_command


@dataclass(frozen=True)
class Service:
    """A backing service DevReady can run as a standard container."""

    key: str
    image: str
    port: int
    env: Dict[str, str]

    @property
    def container(self) -> str:
        return f"devready-{self.key}"


# Standard images + dev credentials. Postgres/Redis creds match env_vars.py's
# DATABASE_URL / REDIS_URL defaults so the app connects without extra config.
KNOWN_SERVICES: Dict[str, Service] = {
    "postgres": Service(
        "postgres", "postgres:16-alpine", 5432,
        {"POSTGRES_USER": "postgres", "POSTGRES_PASSWORD": "postgres", "POSTGRES_DB": "app_dev"},
    ),
    "redis": Service("redis", "redis:7-alpine", 6379, {}),
    "mysql": Service(
        "mysql", "mysql:8", 3306,
        {"MYSQL_ROOT_PASSWORD": "root", "MYSQL_DATABASE": "app_dev",
         "MYSQL_USER": "app", "MYSQL_PASSWORD": "app"},
    ),
    "mongo": Service("mongo", "mongo:7", 27017, {}),
}

# Dependency / config tokens that imply each service (matched in a lowercased
# blob of the project's dep + env files). Kept specific to avoid false positives.
_SERVICE_SIGNALS: Dict[str, tuple] = {
    "postgres": (
        "psycopg", "asyncpg", "pg8000", "postgresql", "postgres://", "postgresql://",
        '"pg"', "'pg'", 'provider = "postgresql"', "provider = 'postgresql'",
    ),
    "redis": ("ioredis", "aioredis", "redis://", '"redis"', "'redis'", "redis-py", "redis="),
    "mysql": (
        "mysql2", "mysqlclient", "pymysql", "mariadb", "mysql://",
        'provider = "mysql"', "provider = 'mysql'",
    ),
    "mongo": ("mongoose", "pymongo", "motor", "mongodb://", "mongodb+srv", "mongo_url", "mongodb_uri"),
}

# Files whose text reveals which services a project talks to.
_SIGNAL_FILES = (
    "package.json", "requirements.txt", "pyproject.toml", "Pipfile", "setup.py",
    "go.mod", "composer.json", "Gemfile", ".env", ".env.example", ".env.sample",
    "prisma/schema.prisma", "schema.prisma", "config/database.yml",
)


def _collect_signal_text(project_dir: Path) -> str:
    """Concatenate the relevant dep/config files into one lowercased blob."""
    parts: List[str] = []
    for name in _SIGNAL_FILES:
        path = project_dir / name
        try:
            if path.is_file():
                parts.append(path.read_text(encoding="utf-8", errors="replace")[:20000])
        except OSError:
            continue
    return "\n".join(parts).lower()


def detect_services(project_dir: Path) -> List[str]:
    """Return the keys of backing services this project appears to need."""
    text = _collect_signal_text(project_dir)
    return [key for key, signals in _SERVICE_SIGNALS.items() if any(s in text for s in signals)]


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


def _wait_for_port(port: int, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(port):
            return True
        time.sleep(1)
    return False


def ensure_services(keys: List[str], env: Optional[dict] = None) -> List[str]:
    """Start each needed service as a container (idempotent).

    ``env`` is forwarded so the ``docker`` command resolves to the chosen engine
    (Podman provides a ``docker`` shim on PATH). Returns the names of containers
    DevReady started, so they can be stopped later.
    """
    started: List[str] = []
    for key in keys:
        svc = KNOWN_SERVICES.get(key)
        if svc is None:
            continue

        if _port_open(svc.port):
            console.print(f"  [muted]{key} already reachable on port {svc.port} — using it.[/muted]")
            continue

        console.print(f"  Starting [bold]{key}[/bold] ({svc.image}) on port {svc.port} (first run pulls the image)…")

        # Reuse a previously-created DevReady container if it exists, else create.
        restarted = run_command(["docker", "start", svc.container], env=env).ok
        if not restarted:
            args = ["docker", "run", "-d", "--name", svc.container, "-p", f"{svc.port}:{svc.port}"]
            for name, value in svc.env.items():
                args += ["-e", f"{name}={value}"]
            args.append(svc.image)
            if not run_command(args, env=env, capture=False).ok:
                console.print(f"  [warning]Couldn't start {key} — the app may fail to connect to it.[/warning]")
                continue

        if _wait_for_port(svc.port):
            console.print(f"  [success]{key} is ready on port {svc.port}.[/success]")
        else:
            console.print(
                f"  [warning]{key} container is up but not accepting connections yet — "
                f"it may need another moment.[/warning]"
            )
        started.append(svc.container)
    return started


def stop_services(containers: List[str], env: Optional[dict] = None) -> None:
    """Stop the given DevReady service containers (best-effort)."""
    for container in containers:
        run_command(["docker", "stop", container], env=env)
