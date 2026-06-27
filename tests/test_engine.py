"""Tests for the Engine's smart preflight (requirements_report).

These verify the "what does this project need vs. what's installed" analysis
that powers the plan shown during ``start`` and ``devready doctor``.
"""

from devready.engine import Engine


def test_requirements_report_python(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests\n")
    report = Engine(project_dir=tmp_path).requirements_report()
    names = [r["name"] for r in report]
    assert "Python" in names
    # Python projects always get an isolated venv row.
    assert any("venv" in n.lower() for n in names)
    # Every row has the expected shape.
    for row in report:
        assert set(row) == {"name", "needs", "have", "ready", "action"}


def test_requirements_report_node_flags_version(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text('{"engines": {"node": ">=99.0"}}')
    # Pretend an old Node is installed so the pinned version isn't satisfied.
    import devready.environment.version_manager as vm

    monkeypatch.setattr(vm, "_node_version", lambda: "22.0.0")
    report = Engine(project_dir=tmp_path).requirements_report()
    node = next(r for r in report if r["name"] == "Node.js")
    assert node["ready"] is False  # 22 does not satisfy >=99
    assert "fnm" in node["action"]


def test_requirements_report_includes_system_packages_and_env(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\n")
    (tmp_path / ".env.example").write_text("API_KEY=\n")
    from devready.ai import ReadmeInsights

    eng = Engine(project_dir=tmp_path)
    eng.insights = ReadmeInsights(system_packages=["ffmpeg"], env_vars={"API_KEY": "key"})
    report = eng.requirements_report()
    names = [r["name"] for r in report]
    assert "ffmpeg" in names          # README system package surfaced
    assert ".env file" in names        # env file row surfaced


def test_requirements_report_empty_for_unknown(tmp_path):
    # A directory with no recognised stack yields no requirement rows.
    assert Engine(project_dir=tmp_path).requirements_report() == []


def test_requirements_report_includes_detected_services(tmp_path):
    # A project that talks to Postgres should surface a 'postgres' service row in
    # the plan, so the user sees DevReady will provision it.
    (tmp_path / "requirements.txt").write_text("psycopg2-binary\nflask\n")
    report = Engine(project_dir=tmp_path).requirements_report()
    assert "postgres" in [r["name"] for r in report]


def test_project_setup_failure_falls_back_to_native(tmp_path, monkeypatch):
    # ROOT FIX: a failing project setup script (e.g. bash setup.sh) must NOT abort
    # the install — _try_project_setup returns False so native install runs, and
    # it doesn't mark the run failed.
    import devready.engine as engine_mod
    from devready.environment import strategies
    from devready.utils import CommandResult

    eng = Engine(project_dir=tmp_path)
    eng.assume_yes = True
    monkeypatch.setattr(
        strategies, "detect_setup_strategies",
        lambda p: [strategies.SetupStrategy("script", ["bash", "setup.sh"], "bash setup.sh", "bash")],
    )
    monkeypatch.setattr(engine_mod, "command_exists", lambda n: True)
    monkeypatch.setattr(engine_mod, "run_command", lambda *a, **k: CommandResult("bash setup.sh", 1))

    assert eng._try_project_setup() is False     # falls through to native setup
    assert eng._install_ok is True               # the script's failure didn't abort the run


def test_project_setup_success_returns_true(tmp_path, monkeypatch):
    import devready.engine as engine_mod
    from devready.environment import strategies
    from devready.utils import CommandResult

    eng = Engine(project_dir=tmp_path)
    eng.assume_yes = True
    monkeypatch.setattr(
        strategies, "detect_setup_strategies",
        lambda p: [strategies.SetupStrategy("makefile", ["make", "setup"], "make setup", "make")],
    )
    monkeypatch.setattr(engine_mod, "command_exists", lambda n: True)
    monkeypatch.setattr(engine_mod, "run_command", lambda *a, **k: CommandResult("make setup", 0))

    assert eng._try_project_setup() is True
    assert eng._project_setup_ran is True


def test_run_brings_up_services_on_relaunch(tmp_path, monkeypatch):
    # The "Run" path must bring up Docker services too (not just the web command),
    # so installing Docker then clicking Run does the full setup.
    import devready.engine as engine_mod

    eng = Engine(project_dir=tmp_path)
    called = {"services": False}
    monkeypatch.setattr(eng, "_bring_up_services", lambda *a, **k: called.__setitem__("services", True))
    monkeypatch.setattr(engine_mod, "detect_stack", lambda p: [])
    monkeypatch.setattr(eng, "_collect_launch_targets", lambda: [])
    monkeypatch.setattr(eng, "_no_server_help", lambda: None)

    eng.run()
    assert called["services"] is True


def test_bring_up_services_runs_compose(tmp_path, monkeypatch):
    import devready.engine as engine_mod
    from devready.utils import CommandResult

    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    eng = Engine(project_dir=tmp_path)
    monkeypatch.setattr(eng, "_ensure_runtime", lambda: ("docker", None))
    monkeypatch.setattr(eng, "_launch_env", lambda: None)
    ran = []
    monkeypatch.setattr(
        engine_mod, "run_command",
        lambda cmd, **k: ran.append(cmd) or CommandResult(command="x", returncode=0),
    )

    eng._bring_up_services()
    assert any("compose" in c and "up" in c for c in ran)


def test_migration_env_loads_project_dotenv(tmp_path, monkeypatch):
    # Migration tools read DATABASE_URL from the env — DevReady must load the
    # project's .env into the migration subprocess (without clobbering PATH).
    import devready.engine as engine_mod

    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql://postgres:postgres@localhost:5432/app_dev\n"
        "# a comment\nPATH=/evil/should/not/win\n"
    )
    eng = Engine(project_dir=tmp_path)
    monkeypatch.setattr(eng, "_launch_env", lambda: {"PATH": "/real/path"})
    env = eng._migration_env()
    assert env["DATABASE_URL"].endswith("/app_dev")
    assert env["PATH"] == "/real/path"  # .env's PATH was ignored


def test_step_migrations_runs_prisma(tmp_path, monkeypatch):
    # A Prisma project should get `prisma generate` + `prisma migrate deploy`.
    import devready.engine as engine_mod
    from devready.ai import ReadmeInsights

    (tmp_path / "prisma").mkdir()
    (tmp_path / "prisma" / "schema.prisma").write_text('datasource db { provider = "postgresql" }')

    from devready.utils import CommandResult

    eng = Engine(project_dir=tmp_path)
    eng.insights = ReadmeInsights()  # no explicit db_commands
    monkeypatch.setattr(eng, "_migration_env", lambda: {"PATH": "x"})

    ran = []
    monkeypatch.setattr(
        engine_mod, "run_command",
        lambda cmd, **k: ran.append(cmd) or CommandResult(command="x", returncode=0),
    )

    eng._step_migrations()
    joined = [" ".join(c) if isinstance(c, list) else c for c in ran]
    assert any("prisma generate" in j for j in joined)
    assert any("prisma migrate deploy" in j for j in joined)


def test_detect_port_from_log_prefers_announced_url(tmp_path):
    eng = Engine(project_dir=tmp_path)
    log = tmp_path / "run.log"
    log.write_text("VITE ready\n  ->  Local:   http://localhost:5173/\n")
    # The announced port wins over the guessed fallback.
    assert eng._detect_port_from_log(log, fallback=3000) == 5173


def test_detect_port_from_log_falls_back_when_silent(tmp_path):
    eng = Engine(project_dir=tmp_path)
    log = tmp_path / "run.log"
    log.write_text("compiling...\nstill working, no url yet\n")
    assert eng._detect_port_from_log(log, fallback=8000) == 8000


def test_scan_build_error_finds_module_not_found(tmp_path):
    eng = Engine(project_dir=tmp_path)
    log = tmp_path / "run.log"
    log.write_text(
        "> next dev\n ready - started server on 0.0.0.0:3000\n"
        "Module not found: Can't resolve 'design-agent'\n  at ./src/x.jsx\n"
    )
    snippet = eng._scan_build_error(log)
    assert snippet and "design-agent" in snippet


def test_scan_build_error_none_on_clean_log(tmp_path):
    eng = Engine(project_dir=tmp_path)
    log = tmp_path / "run.log"
    log.write_text(
        "> next dev\n ready - started server on 0.0.0.0:3000\n- compiled successfully\n",
        encoding="utf-8",
    )
    assert eng._scan_build_error(log) is None


def test_launch_env_uses_pinned_node_bin(tmp_path, monkeypatch):
    # When a project pins a Node version the system doesn't meet, the launch env
    # must put that Node's bin dir first on PATH (so `npm run dev` doesn't run on
    # the wrong Node and crash, as gradio did).
    import devready.engine as engine_mod
    from devready.detectors import DetectionResult

    bin_dir = tmp_path / "node24bin"
    bin_dir.mkdir()
    eng = Engine(project_dir=tmp_path)
    eng.detections = [
        DetectionResult(language="Node.js", version="24.0", frameworks=[], package_files=["package.json"])
    ]

    monkeypatch.setattr(engine_mod.version_manager, "_node_satisfies", lambda v: False)
    monkeypatch.setattr(engine_mod.version_manager, "_fnm_node_bin_dir", lambda v: str(bin_dir))

    env = eng._launch_env()
    assert env is not None
    assert env["PATH"].startswith(str(bin_dir))
    # And it persisted the bin dir so `devready run` can relaunch with it.
    assert eng._read_state().get("node_bin_dir") == str(bin_dir)


def test_launch_env_none_when_system_node_is_fine(tmp_path, monkeypatch):
    import devready.engine as engine_mod
    from devready.detectors import DetectionResult

    eng = Engine(project_dir=tmp_path)
    eng.detections = [
        DetectionResult(language="Node.js", version="20", frameworks=[], package_files=["package.json"])
    ]
    monkeypatch.setattr(engine_mod.version_manager, "_node_satisfies", lambda v: True)
    monkeypatch.setattr(engine_mod.version_manager, "needs_bash_script_shell", lambda p: None)
    assert eng._launch_env() is None


def test_launch_env_uses_bash_for_shell_script_projects(tmp_path, monkeypatch):
    # A project whose `npm run dev` is a Unix shell script must launch through
    # bash on Windows, even when the system Node is fine (no pinned-Node env).
    import devready.engine as engine_mod

    eng = Engine(project_dir=tmp_path)
    eng.detections = []  # no pinned Node -> env would otherwise be None
    monkeypatch.setattr(engine_mod.version_manager, "needs_bash_script_shell", lambda p: r"C:\Git\bash.exe")

    env = eng._launch_env()
    assert env is not None
    assert env["npm_config_script_shell"] == r"C:\Git\bash.exe"
