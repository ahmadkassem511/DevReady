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


def _stub_incompatible_check(monkeypatch):
    """Force _step_system_check to see an incompatible machine (no CUDA GPU)."""
    from devready.environment import system_check as sc

    hw = sc.HardwareInfo(
        os_name="Windows 10", os_arch="amd64", cpu_cores=4, cpu_model="x",
        ram_gb=7.0, disk_free_gb=10.0,
        gpu_model="Intel(R) HD Graphics 3000", gpu_cuda_capable=False,
    )
    req = sc.SystemRequirements(gpu_required=True, gpu_cuda_required=True, source="regex")
    report = sc.CompatibilityReport(
        compatible=False,
        checks=[sc.CheckResult("GPU", "error", "Intel HD", "CUDA-capable GPU", "no cuda")],
        hw=hw, req=req,
    )
    monkeypatch.setattr(sc, "get_hardware_info", lambda *a, **k: hw)
    monkeypatch.setattr(sc, "extract_requirements", lambda *a, **k: req)
    monkeypatch.setattr(sc, "check_compatibility", lambda *a, **k: report)
    monkeypatch.setattr(sc, "print_report", lambda *a, **k: None)


def test_incompatible_check_prompts_and_continues_on_yes(tmp_path, monkeypatch):
    # Interactive CLI: a failed check must ASK, and a "yes" overrides so the
    # install proceeds (instead of forcing a re-run with --yes).
    (tmp_path / "requirements.txt").write_text("torch\n")
    _stub_incompatible_check(monkeypatch)
    eng = Engine(project_dir=tmp_path)
    asked = {}
    def fake_confirm(prompt, default_yes=True):
        asked["prompt"] = prompt
        asked["default_yes"] = default_yes
        return True
    eng._confirm = fake_confirm

    eng._step_system_check()
    assert asked.get("default_yes") is False   # defaults to No (safe)
    assert eng._compat_ok is True              # user chose to continue anyway


def test_incompatible_check_blocks_on_no(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("torch\n")
    _stub_incompatible_check(monkeypatch)
    eng = Engine(project_dir=tmp_path)
    eng._confirm = lambda prompt, default_yes=True: False

    eng._step_system_check()
    assert eng._compat_ok is False             # declined -> start() will abort


def test_incompatible_check_under_yes_does_not_prompt(tmp_path, monkeypatch):
    # Unattended (--yes / GUI): never prompt; proceed. _compat_ok stays False but
    # the start() gate lets --yes through.
    (tmp_path / "requirements.txt").write_text("torch\n")
    _stub_incompatible_check(monkeypatch)
    eng = Engine(project_dir=tmp_path, assume_yes=True)
    prompted = {"v": False}
    eng._confirm = lambda *a, **k: prompted.__setitem__("v", True) or True

    eng._step_system_check()
    assert prompted["v"] is False
    assert eng._compat_ok is False


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


def test_resolve_server_command_node_root_entry(tmp_path, monkeypatch):
    # openclaw case: `openclaw` isn't on PATH, but openclaw.mjs is the entry.
    import devready.engine as engine_mod
    (tmp_path / "openclaw.mjs").write_text("// entry\n")
    monkeypatch.setattr(engine_mod, "command_exists", lambda h: False)
    eng = Engine(project_dir=tmp_path)
    assert eng._resolve_server_command("openclaw gateway run") == [
        "node", "openclaw.mjs", "gateway", "run",
    ]


def test_resolve_server_command_on_path(tmp_path, monkeypatch):
    import devready.engine as engine_mod
    monkeypatch.setattr(engine_mod, "command_exists", lambda h: h == "myserver")
    eng = Engine(project_dir=tmp_path)
    assert eng._resolve_server_command("myserver serve --port 9000") == [
        "myserver", "serve", "--port", "9000",
    ]


def test_resolve_server_command_pnpm_workspace_bin(tmp_path, monkeypatch):
    import devready.engine as engine_mod
    (tmp_path / "package.json").write_text('{"name": "root"}')
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'ui'\n")
    monkeypatch.setattr(engine_mod, "command_exists", lambda h: h == "pnpm")
    eng = Engine(project_dir=tmp_path)
    assert eng._resolve_server_command("openclaw gateway run") == [
        "pnpm", "exec", "openclaw", "gateway", "run",
    ]


def test_resolve_project_cli_finds_venv_entry_point(tmp_path, monkeypatch):
    # After a published-package install, the CLI lives in .venv/Scripts (Windows)
    # or .venv/bin — `open-webui serve` must resolve to that full path.
    import sys as _sys
    bin_dir = tmp_path / ".venv" / ("Scripts" if _sys.platform == "win32" else "bin")
    bin_dir.mkdir(parents=True)
    exe = bin_dir / ("open-webui.exe" if _sys.platform == "win32" else "open-webui")
    exe.write_text("")
    eng = Engine(project_dir=tmp_path)
    resolved = eng._resolve_project_cli("open-webui serve")
    assert resolved == [str(exe), "serve"]
    # Unknown CLI -> None; unsafe -> None.
    assert eng._resolve_project_cli("other-tool serve") is None
    assert eng._resolve_project_cli("open-webui serve && rm -rf /") is None


def test_has_runnable_web_command_accepts_venv_cli(tmp_path):
    import sys as _sys
    bin_dir = tmp_path / ".venv" / ("Scripts" if _sys.platform == "win32" else "bin")
    bin_dir.mkdir(parents=True)
    (bin_dir / ("open-webui.exe" if _sys.platform == "win32" else "open-webui")).write_text("")
    eng = Engine(project_dir=tmp_path)
    assert eng._has_runnable_web_command(
        {"has_web_ui": True, "launch_command": "open-webui serve"}
    ) is True
    assert eng._has_runnable_web_command(
        {"has_web_ui": True, "launch_command": "not-installed serve"}
    ) is False


def test_resolve_server_command_unresolvable(tmp_path, monkeypatch):
    import devready.engine as engine_mod
    monkeypatch.setattr(engine_mod, "command_exists", lambda h: False)
    eng = Engine(project_dir=tmp_path)
    # No PATH match, no root .mjs/.js, no package.json -> can't resolve.
    assert eng._resolve_server_command("openclaw gateway run") is None


def test_compose_stop_passes_profile(tmp_path, monkeypatch):
    # NextChat's services are gated behind a profile; `compose down` without it
    # leaves the app container running (seen live on port 3000). stop() must
    # pass the detected --profile so the app is actually stopped.
    import devready.engine as engine_mod
    from devready.utils import CommandResult

    (tmp_path / "docker-compose.yml").write_text(
        'services:\n  web:\n    profiles: [ "no-proxy" ]\n    image: x\n    ports:\n      - 3000:3000\n'
    )
    (tmp_path / ".env").write_text("FOO=bar\n")
    eng = Engine(project_dir=tmp_path)
    eng._write_state(docker=True, processes=[])
    monkeypatch.setattr(engine_mod, "command_exists", lambda n: n == "docker")
    monkeypatch.setattr(Engine, "_docker_compose_v2", staticmethod(lambda: True))
    ran = []
    monkeypatch.setattr(
        engine_mod, "run_command",
        lambda cmd, **k: ran.append(list(cmd)) or CommandResult("x", 0),
    )
    eng.stop()
    down = next((c for c in ran if "down" in c), None)
    assert down is not None
    assert "--profile" in down and "no-proxy" in down   # the gated app is targeted
    assert "--env-file" in down                          # .env passed so vars resolve


def test_compose_web_port_reuses_http_serving_port(tmp_path, monkeypatch):
    # NextChat: compose serves the app on 3000; a prior double-run drifted the
    # saved web target to 3001. _compose_web_port must return 3000 (the HTTP
    # port) regardless of the drifted target port, self-healing the drift.
    eng = Engine(project_dir=tmp_path)
    eng._compose_started = True
    eng._compose_ports = {3000}
    monkeypatch.setattr(eng, "_port_serves_http", lambda p: p == 3000)
    assert eng._compose_web_port(3001) == 3000   # drifted target -> heals to 3000
    assert eng._compose_web_port(3000) == 3000
    assert eng._compose_web_port(None) == 3000


def test_compose_web_port_ignores_db_only_stack(tmp_path, monkeypatch):
    # Compose runs only a database (5432, no HTTP); the web app is a real npm
    # run on 3000 — the source launch MUST proceed, so this returns None.
    import devready.engine as em
    monkeypatch.setattr(em.time, "sleep", lambda s: None)  # instant settle loop
    eng = Engine(project_dir=tmp_path)
    eng._compose_started = True
    eng._compose_ports = {5432}
    monkeypatch.setattr(eng, "_port_serves_http", lambda p: False)
    assert eng._compose_web_port(3000) is None


def test_launch_reuses_compose_app_instead_of_second_copy(tmp_path, monkeypatch):
    # The end-to-end guard: when the compose app already serves the port, the
    # saved `npm run dev` target must NOT be spawned (which would drift to 3001).
    eng = Engine(project_dir=tmp_path)
    eng._compose_started = True
    eng._compose_ports = {3000}
    monkeypatch.setattr(eng, "_port_serves_http", lambda p: p == 3000)
    monkeypatch.setattr(
        eng, "_spawn_and_check",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not start a second copy")),
    )
    monkeypatch.setattr(eng, "_announce_running", lambda records: [
        f"http://localhost:{r['port']}" for r in records if r.get("port")
    ])
    served = eng._launch_targets([{
        "name": "root", "cwd": str(tmp_path),
        "command": ["npm", "run", "dev"], "port": 3001,  # drifted saved port
    }])
    assert served == ["http://localhost:3000"]  # reused the container's port
    assert eng._read_state()["processes"][0]["port"] == 3000


def test_argv_needs_docker(tmp_path):
    eng = Engine(project_dir=tmp_path)
    assert eng._argv_needs_docker(["docker", "run", "-p", "8080:8080", "img"]) is True
    assert eng._argv_needs_docker(["docker-compose", "up"]) is True
    assert eng._argv_needs_docker(["podman", "run", "img"]) is True
    assert eng._argv_needs_docker(["npm", "run", "dev"]) is False
    assert eng._argv_needs_docker([]) is False


def test_run_blocks_docker_relaunch_without_engine(tmp_path, monkeypatch):
    # Seen live (neko): `devready run` printed "No container engine available"
    # and then launched `docker run …` anyway — doomed, and the output
    # contradicted itself. Without an engine, a docker relaunch must not spawn.
    eng = Engine(project_dir=tmp_path)
    eng._write_state(last_launch=[{
        "name": "root", "cwd": str(tmp_path),
        "command": ["docker", "run", "-p", "8080:8080", "m1k1o/neko"], "port": 8080,
    }])
    monkeypatch.setattr(eng, "_bring_up_services", lambda **k: None)
    monkeypatch.setattr(eng, "_ensure_runtime", lambda: (None, None))
    monkeypatch.setattr(
        eng, "_launch_targets",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not launch without an engine")),
    )
    eng.run()  # returns after the warning — no launch attempted


def test_docker_run_image_extraction(tmp_path):
    eng = Engine(project_dir=tmp_path)
    # neko-style: value-taking flags before the image.
    assert eng._docker_run_image(
        ["docker", "run", "-d", "-p", "8080:8080", "-e", "NEKO_PASSWORD=neko",
         "--shm-size", "2gb", "ghcr.io/m1k1o/neko/firefox:latest"]
    ) == "ghcr.io/m1k1o/neko/firefox:latest"
    assert eng._docker_run_image(
        ["docker", "run", "-d", "-p", "8088:80", "--name", "welcome", "docker/welcome-to-docker"]
    ) == "docker/welcome-to-docker"
    assert eng._docker_run_image(["docker", "run", "--rm", "img", "echo", "hi"]) == "img"
    assert eng._docker_run_image(["docker", "compose", "up"]) is None
    assert eng._docker_run_image(["npm", "run", "dev"]) is None


def test_purge_docker_artifacts_removes_containers_and_images(tmp_path, monkeypatch):
    # Deleting a project must free its Docker footprint: rm -f its containers
    # and rmi the image its saved launch command pulled.
    import devready.engine as engine_mod
    from devready.utils import CommandResult

    eng = Engine(project_dir=tmp_path)
    eng._write_state(
        docker_containers=["welcome-to-docker"],
        last_launch=[{
            "name": "root", "cwd": str(tmp_path),
            "command": ["docker", "run", "-d", "-p", "8088:80", "--name",
                        "welcome-to-docker", "docker/welcome-to-docker"],
            "port": 8088,
        }],
    )
    ran = []
    monkeypatch.setattr(engine_mod, "command_exists", lambda n: n == "docker")
    monkeypatch.setattr(
        engine_mod, "run_command",
        lambda cmd, **k: ran.append(list(cmd)) or CommandResult("x", 0),
    )
    eng.purge_docker_artifacts()
    assert ["docker", "rm", "-f", "welcome-to-docker"] in ran
    assert ["docker", "rmi", "docker/welcome-to-docker"] in ran


def test_docker_container_name_extraction(tmp_path):
    eng = Engine(project_dir=tmp_path)
    assert eng._docker_container_name(
        ["docker", "run", "-d", "-p", "8088:80", "--name", "welcome", "img"]
    ) == "welcome"
    assert eng._docker_container_name(
        ["docker", "run", "-d", "--name=welcome", "img"]
    ) == "welcome"
    assert eng._docker_container_name(["docker", "compose", "up", "-d"]) is None
    assert eng._docker_container_name(["npm", "run", "dev"]) is None
    assert eng._docker_container_name(["docker", "run", "img"]) is None  # unnamed


def test_stop_stops_recorded_app_container(tmp_path, monkeypatch):
    # A guided `docker run --name X` launch: the launcher pid dies instantly,
    # so stop must stop the CONTAINER by name — previously it said "No running
    # processes recorded" and left the app running.
    import devready.engine as engine_mod
    from devready.utils import CommandResult

    eng = Engine(project_dir=tmp_path)
    eng._write_state(docker_containers=["welcome-to-docker"], processes=[])
    ran = []
    monkeypatch.setattr(engine_mod, "command_exists", lambda n: n == "docker")
    monkeypatch.setattr(
        engine_mod, "run_command",
        lambda cmd, **k: ran.append(list(cmd)) or CommandResult("x", 0),
    )
    eng.stop()
    assert ["docker", "stop", "welcome-to-docker"] in ran


def test_status_finds_container_via_podman_shim(tmp_path, monkeypatch):
    # On a no-admin machine `docker` isn't a real binary — only the Podman shim
    # at ~/.devready/bin/docker(.cmd) provides it, and that dir isn't on a fresh
    # `devready status` process's PATH unless we add it (same as stop() does).
    import devready.engine as engine_mod

    eng = Engine(project_dir=tmp_path)
    eng._write_state(docker_containers=["welcome-to-docker"], processes=[])

    fake_home = tmp_path / "home"
    shim_dir = fake_home / ".devready" / "bin"
    shim_dir.mkdir(parents=True)
    (shim_dir / "docker").write_text("#!/bin/sh\nexec podman \"$@\"\n")

    monkeypatch.setattr(engine_mod.Path, "home", lambda: fake_home)
    monkeypatch.setattr(engine_mod, "command_exists", lambda n: False)  # no real docker
    checked = []
    monkeypatch.setattr(
        eng, "_docker_container_running",
        lambda name, env=None: checked.append(name) or True,
    )

    eng.status()
    # Without the shim-dir fallback, has_docker would be False and the running
    # check never reached — checked would stay empty and the row would
    # (wrongly) say "not running" even though Podman has it up.
    assert checked == ["welcome-to-docker"]


def test_relaunch_existing_container_uses_docker_start(tmp_path, monkeypatch):
    # Re-running the saved `docker run --name X` would fail with "name already
    # in use" — an existing container must be relaunched with `docker start`.
    eng = Engine(project_dir=tmp_path)
    spawned = []

    def fake_spawn(target, **kwargs):
        spawned.append(list(target["command"]))
        return {"name": target["name"], "pid": 123, "command": target["command"],
                "port": 8088, "cwd": target["cwd"]}

    monkeypatch.setattr(eng, "_spawn_and_check", fake_spawn)
    monkeypatch.setattr(eng, "_docker_container_exists", lambda name, env=None: True)
    monkeypatch.setattr(eng, "_announce_running", lambda records: ["http://localhost:8088"])

    eng._write_state(needs_container_engine=True)  # stale verdict from services step
    original = ["docker", "run", "-d", "-p", "8088:80", "--name", "welcome", "img"]
    eng._launch_targets([{"name": "root", "cwd": str(tmp_path), "command": original, "port": 8088}])

    assert spawned == [["docker", "start", "welcome"]]
    state = eng._read_state()
    assert state["docker_containers"] == ["welcome"]
    # The ORIGINAL run command stays persisted so a deleted container can be
    # recreated, and `last_launch` survives `devready stop` for relaunching.
    assert state["processes"][0]["command"] == original
    assert state["last_launch"][0]["command"] == original
    # A serving docker launch proves the engine works — the stale "install
    # Docker" verdict must be cleared so the CLI/GUI stop contradicting reality.
    assert state["needs_container_engine"] is False


def test_src_tauri_subproject_skipped(tmp_path, monkeypatch):
    # NextChat-style repo: Next.js web app + src-tauri desktop packaging.
    # Building the Tauri shell needs MSVC/WebView2 and is NOT needed to run
    # the web app — it must be skipped, not attempted-and-failed.
    import devready.engine as engine_mod

    (tmp_path / "package.json").write_text('{"name": "nextchat"}')
    tauri = tmp_path / "src-tauri"
    tauri.mkdir()
    (tauri / "Cargo.toml").write_text('[package]\nname = "app"\n')

    eng = Engine(project_dir=tmp_path)
    monkeypatch.setattr(
        engine_mod.version_manager, "setup_environment",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not build src-tauri")),
    )
    eng._setup_subprojects()  # skips src-tauri, never calls setup
    # And the launch side must not offer `cargo run` for the desktop shell.
    assert eng._is_tauri_packaging_dir(tauri) is True
    assert eng._is_tauri_packaging_dir(tmp_path / "frontend") is False


def test_resolve_launch_command_heals_stale_path(tmp_path, monkeypatch):
    # Seen live (NextChat): the GUI server's PATH predated the Node install, so
    # `npm run dev` -> WinError 2. When argv[0] doesn't resolve, the launcher
    # must refresh PATH from the registry/common dirs and re-resolve.
    import devready.engine as engine_mod

    eng = Engine(project_dir=tmp_path)
    monkeypatch.setattr(engine_mod.sys, "platform", "win32")

    calls = {"refreshed": False}

    def fake_resolve(command, path=None):
        if calls["refreshed"]:
            return [r"C:\nodejs\npm.CMD", *command[1:]]
        return command  # unresolved before the refresh

    def fake_refresh():
        calls["refreshed"] = True

    monkeypatch.setattr(engine_mod, "_resolve_windows_executable", fake_resolve)
    import devready.environment.system_deps as sd
    monkeypatch.setattr(sd, "refresh_path", fake_refresh)

    env = {"PATH": r"C:\stale", "npm_config_script_shell": "bash"}
    resolved, new_env = eng._resolve_launch_command(["npm", "run", "dev"], env)

    assert resolved[0] == r"C:\nodejs\npm.CMD"
    assert calls["refreshed"] is True
    # env PATH extended so npm's own child (node) resolves inside the launch.
    assert new_env["PATH"].startswith(r"C:\stale")
    assert len(new_env["PATH"]) > len(r"C:\stale")
    assert new_env["npm_config_script_shell"] == "bash"  # other keys preserved


def test_subprojects_skipped_after_published_install(tmp_path, monkeypatch):
    # Once the official published package is installed, the source tree's
    # components (e.g. Open WebUI's backend/) are the code the wheel already
    # ships — setting them up again is a redundant multi-gigabyte install.
    from devready.environment import version_manager as vm

    marker_dir = tmp_path / ".venv"
    marker_dir.mkdir()
    (marker_dir / vm._PUBLISHED_MARKER).write_text("open-webui")
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "requirements.txt").write_text("fastapi\n")

    eng = Engine(project_dir=tmp_path)
    monkeypatch.setattr(
        eng, "_detect_subprojects",
        lambda: (_ for _ in ()).throw(AssertionError("must not scan sub-projects")),
    )
    eng._setup_subprojects()  # returns early — never even scans


def test_collect_launch_targets_adds_server_from_guide(tmp_path, monkeypatch):
    import devready.engine as engine_mod
    (tmp_path / "openclaw.mjs").write_text("// entry\n")
    (tmp_path / "package.json").write_text('{"name": "root"}')
    monkeypatch.setattr(engine_mod, "command_exists", lambda h: False)
    eng = Engine(project_dir=tmp_path)
    monkeypatch.setattr(eng, "_resolve_launch", lambda: (None, None))
    monkeypatch.setattr(eng, "_detect_subprojects", lambda: [])
    targets = eng._collect_launch_targets(guide={"server_command": "openclaw gateway run"})
    server = next((t for t in targets if t["name"] == "server"), None)
    assert server is not None
    assert server["command"] == ["node", "openclaw.mjs", "gateway", "run"]


def test_collect_launch_targets_skips_unsafe_server_command(tmp_path, monkeypatch):
    import devready.engine as engine_mod
    monkeypatch.setattr(engine_mod, "command_exists", lambda h: False)
    eng = Engine(project_dir=tmp_path)
    monkeypatch.setattr(eng, "_resolve_launch", lambda: (None, None))
    monkeypatch.setattr(eng, "_detect_subprojects", lambda: [])
    targets = eng._collect_launch_targets(guide={"server_command": "foo && rm -rf /"})
    assert not any(t["name"] == "server" for t in targets)


def test_report_compose_status_lists_running_containers(tmp_path, monkeypatch):
    # After `up`, DevReady must confirm containers are actually running and show
    # them (so "nothing in Docker Desktop" is never a silent mystery).
    import devready.engine as engine_mod
    from devready.utils import CommandResult

    def fake_run(cmd, **kw):
        if cmd[-2:] == ["ps", "-q"]:
            return CommandResult("ps -q", 0, stdout="abc123\ndef456\n")
        if cmd[-1] == "ps":
            return CommandResult("ps", 0, stdout="NAME     STATUS\napi      Up 3s\ndb       Up 3s\n")
        return CommandResult("x", 0)

    monkeypatch.setattr(engine_mod, "run_command", fake_run)
    eng = Engine(project_dir=tmp_path)
    assert eng._report_compose_status(["docker", "compose"], None) is True


def test_report_compose_status_flags_crashed_container(tmp_path, monkeypatch):
    # `up` succeeded but nothing stayed running -> report False and diagnose.
    import devready.engine as engine_mod
    from devready.utils import CommandResult

    def fake_run(cmd, **kw):
        if cmd[-2:] == ["ps", "-q"]:
            return CommandResult("ps -q", 0, stdout="")          # nothing running
        if cmd[-2:] == ["ps", "-a"]:
            return CommandResult("ps -a", 0, stdout="NAME  STATUS\napi   Exited (1)\n")
        if cmd[-3:] == ["logs", "--tail", "20"]:
            return CommandResult("logs", 0, stdout="Error: missing DATABASE_URL\n")
        if cmd[-1] == "ps":
            return CommandResult("ps", 0, stdout="")
        return CommandResult("x", 0)

    monkeypatch.setattr(engine_mod, "run_command", fake_run)
    eng = Engine(project_dir=tmp_path)
    assert eng._report_compose_status(["docker", "compose"], None) is False


def test_needs_interactive_setup_detects_onboarding(tmp_path):
    # openclaw's gateway prints this and never binds a port until onboarded.
    eng = Engine(project_dir=tmp_path)
    log = tmp_path / "run.log"
    log.write_text(
        "> openclaw@2026 dev\n"
        "Onboarding needs an interactive TTY. Use `openclaw onboard "
        "--non-interactive --accept-risk ...` for automation.\n"
    )
    assert eng._needs_interactive_setup(log) is True


def test_needs_interactive_setup_false_for_ordinary_server(tmp_path):
    eng = Engine(project_dir=tmp_path)
    log = tmp_path / "run.log"
    log.write_text("VITE ready\n  ->  Local: http://localhost:5173/\nrun `npm test` to test\n")
    assert eng._needs_interactive_setup(log) is False


def test_detect_port_from_log_strips_ansi_colours(tmp_path):
    # Vite prints its URL wrapped in ANSI colour codes — the port must still be
    # detected (the openclaw case, where a live 5173 server looked "not serving").
    eng = Engine(project_dir=tmp_path)
    log = tmp_path / "run.log"
    log.write_text(
        "  \x1b[32m➜\x1b[39m  \x1b[1mLocal\x1b[22m:   "
        "\x1b[36mhttp://localhost:\x1b[1m5173\x1b[22m\x1b[36m/\x1b[39m\n",
        encoding="utf-8",
    )
    assert eng._detect_port_from_log(log, fallback=3000) == 5173


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


# --- devready update ---------------------------------------------------------

def _git(cwd, *args):
    import subprocess
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


def _make_origin_and_clone(tmp_path, filename="package.json", content='{"name":"app"}'):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q")
    _git(origin, "config", "user.email", "t@t")
    _git(origin, "config", "user.name", "t")
    (origin / filename).write_text(content)
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "init")
    proj = tmp_path / "proj"
    _git(tmp_path, "clone", "-q", str(origin), str(proj))
    _git(proj, "config", "user.email", "t@t")
    _git(proj, "config", "user.name", "t")
    return origin, proj


def test_update_pulls_and_reinstalls_changed_deps(tmp_path, monkeypatch):
    import devready.engine as engine_mod

    origin, proj = _make_origin_and_clone(tmp_path)
    # Upstream gains a commit that changes the Node dependency file.
    (origin / "package.json").write_text('{"name":"app","dependencies":{"x":"1"}}')
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "bump deps")

    eng = Engine(project_dir=proj)
    setup_calls, lifecycle = [], []
    monkeypatch.setattr(
        engine_mod.version_manager, "setup_environment",
        lambda pdir, det, healer: setup_calls.append(det.language) or [],
    )
    monkeypatch.setattr(eng, "_step_migrations", lambda header=True: lifecycle.append("migrate"))
    monkeypatch.setattr(eng, "stop", lambda: lifecycle.append("stop"))
    monkeypatch.setattr(eng, "run", lambda: lifecycle.append("run"))

    assert eng.update() is True
    assert setup_calls == ["Node.js"]          # deps changed -> re-install
    assert lifecycle == ["migrate", "stop", "run"]  # then restart
    # And the pull really happened.
    assert "dependencies" in (proj / "package.json").read_text()


def test_update_skips_reinstall_when_no_dep_change(tmp_path, monkeypatch):
    import devready.engine as engine_mod

    origin, proj = _make_origin_and_clone(tmp_path)
    (origin / "readme.md").write_text("docs only\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "docs")

    eng = Engine(project_dir=proj)
    monkeypatch.setattr(
        engine_mod.version_manager, "setup_environment",
        lambda *a: (_ for _ in ()).throw(AssertionError("docs-only change must not re-install")),
    )
    lifecycle = []
    monkeypatch.setattr(eng, "stop", lambda: lifecycle.append("stop"))
    monkeypatch.setattr(eng, "run", lambda: lifecycle.append("run"))

    assert eng.update() is True
    assert lifecycle == ["stop", "run"]


def test_update_refuses_diverged_history(tmp_path, monkeypatch):
    origin, proj = _make_origin_and_clone(tmp_path)
    # Local commit + different upstream commit -> ff-only must refuse.
    (proj / "local.txt").write_text("mine")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-q", "-m", "local")
    (origin / "package.json").write_text('{"name":"app","v":2}')
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "upstream")

    eng = Engine(project_dir=proj)
    monkeypatch.setattr(eng, "run", lambda: (_ for _ in ()).throw(AssertionError("must not restart")))
    assert eng.update() is False
    assert (proj / "local.txt").exists()  # local work untouched


def test_update_requires_git_repo(tmp_path):
    proj = tmp_path / "notgit"
    proj.mkdir()
    assert Engine(project_dir=proj).update() is False


def test_update_upgrades_published_package(tmp_path, monkeypatch):
    import devready.engine as engine_mod
    from devready.environment import version_manager as vm
    from devready.utils import CommandResult

    origin, proj = _make_origin_and_clone(tmp_path)
    marker_dir = proj / ".venv"
    marker_dir.mkdir()
    (marker_dir / vm._PUBLISHED_MARKER).write_text("open-webui")

    eng = Engine(project_dir=proj)
    teed = []
    monkeypatch.setattr(
        engine_mod, "run_command_teed",
        lambda cmd, **k: teed.append(list(cmd)) or CommandResult("x", 0),
    )
    monkeypatch.setattr(eng, "_step_migrations", lambda header=True: None)
    monkeypatch.setattr(eng, "stop", lambda: None)
    monkeypatch.setattr(eng, "run", lambda: None)

    assert eng.update() is True
    # The wheel IS the app: pip install --upgrade <name> must have run.
    assert any(c[-3:] == ["install", "--upgrade", "open-webui"] for c in teed)


def test_dep_files_changed_mapping(tmp_path):
    eng = Engine(project_dir=tmp_path)
    assert eng._dep_files_changed("Python", ["backend/requirements.txt"]) is True
    assert eng._dep_files_changed("Node.js", ["yarn.lock"]) is True
    assert eng._dep_files_changed("Node.js", ["src/app.ts"]) is False
    assert eng._dep_files_changed("Python", ["docs/readme.md"]) is False
