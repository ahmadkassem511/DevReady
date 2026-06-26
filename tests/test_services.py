"""Tests for backing-service detection and provisioning."""

from devready.environment.services import (
    KNOWN_SERVICES,
    detect_services,
    ensure_services,
)
from devready.utils import CommandResult


# -- detection ---------------------------------------------------------------
def test_detect_postgres_from_python_dep(tmp_path):
    (tmp_path / "requirements.txt").write_text("psycopg2-binary==2.9\nflask\n")
    assert "postgres" in detect_services(tmp_path)


def test_detect_redis_and_postgres_from_package_json(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"pg": "^8.0", "ioredis": "^5.0"}}')
    found = detect_services(tmp_path)
    assert "postgres" in found and "redis" in found


def test_detect_mysql_from_env_url(tmp_path):
    (tmp_path / ".env.example").write_text("DATABASE_URL=mysql://user:pass@localhost:3306/db\n")
    assert "mysql" in detect_services(tmp_path)


def test_detect_mongo_from_dep(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"mongoose": "^8.0"}}')
    assert "mongo" in detect_services(tmp_path)


def test_detect_none_for_static_frontend(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"react": "^18", "vite": "^5"}}')
    assert detect_services(tmp_path) == []


def test_known_services_postgres_matches_env_defaults():
    pg = KNOWN_SERVICES["postgres"]
    assert pg.port == 5432
    assert pg.container == "devready-postgres"
    # Creds line up with env_vars.py's DATABASE_URL default (postgres/postgres/app_dev).
    assert pg.env["POSTGRES_USER"] == "postgres"
    assert pg.env["POSTGRES_DB"] == "app_dev"


# -- provisioning ------------------------------------------------------------
def test_ensure_services_reuses_open_port(monkeypatch):
    import devready.environment.services as svc

    monkeypatch.setattr(svc, "_port_open", lambda *a, **k: True)  # already running
    ran = []
    monkeypatch.setattr(svc, "run_command", lambda *a, **k: ran.append(a))
    assert svc.ensure_services(["postgres"]) == []  # nothing to start
    assert ran == []


def test_ensure_services_starts_container(monkeypatch):
    import devready.environment.services as svc

    state = {"open": False}
    monkeypatch.setattr(svc, "_port_open", lambda *a, **k: state["open"])

    cmds = []

    def fake_run(cmd, **kwargs):
        cmds.append(cmd)
        if cmd[:2] == ["docker", "start"]:
            return CommandResult(command="x", returncode=1)  # no existing container
        if cmd[:2] == ["docker", "run"]:
            state["open"] = True  # container is now listening
            return CommandResult(command="x", returncode=0)
        return CommandResult(command="x", returncode=0)

    monkeypatch.setattr(svc, "run_command", fake_run)
    started = svc.ensure_services(["redis"])
    assert started == ["devready-redis"]
    assert any(c[:2] == ["docker", "run"] for c in cmds)
    # The run command targets the right image and port mapping.
    run_cmd = next(c for c in cmds if c[:2] == ["docker", "run"])
    assert "redis:7-alpine" in run_cmd
    assert "6379:6379" in run_cmd
