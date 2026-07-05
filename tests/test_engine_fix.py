"""Tests for `devready fix` — the runtime doctor."""

from devready.engine import Engine


def test_fix_refuses_when_nothing_set_up(tmp_path):
    assert Engine(project_dir=tmp_path).fix() is False


def test_fix_reinstalls_broken_python_venv(tmp_path, monkeypatch):
    import devready.engine as engine_mod

    (tmp_path / "requirements.txt").write_text("flask\n")
    eng = Engine(project_dir=tmp_path)
    eng._write_state(last_launch=[{"name": "root", "cwd": str(tmp_path),
                                    "command": ["python", "app.py"], "port": 5000}])
    # .venv missing entirely -> looks broken.
    calls = []
    monkeypatch.setattr(
        engine_mod.version_manager, "setup_environment",
        lambda pdir, det, healer: calls.append(det.language) or [],
    )
    monkeypatch.setattr(eng, "_diagnose_stopped_containers", lambda state: [])
    monkeypatch.setattr(eng, "stop", lambda: None)
    monkeypatch.setattr(eng, "run", lambda: None)

    assert eng.fix() is True
    assert calls == ["Python"]


def test_fix_reinstalls_missing_node_modules(tmp_path, monkeypatch):
    import devready.engine as engine_mod

    (tmp_path / "package.json").write_text('{"name":"app"}')
    eng = Engine(project_dir=tmp_path)
    eng._write_state(last_launch=[{"name": "root", "cwd": str(tmp_path),
                                    "command": ["npm", "run", "dev"], "port": 3000}])
    calls = []
    monkeypatch.setattr(
        engine_mod.version_manager, "setup_environment",
        lambda pdir, det, healer: calls.append(det.language) or [],
    )
    monkeypatch.setattr(eng, "_diagnose_stopped_containers", lambda state: [])
    monkeypatch.setattr(eng, "stop", lambda: None)
    monkeypatch.setattr(eng, "run", lambda: None)

    assert eng.fix() is True
    assert calls == ["Node.js"]

    # node_modules present -> not broken, no reinstall.
    (tmp_path / "node_modules").mkdir()
    calls.clear()
    assert eng.fix() is True
    assert calls == []


def test_fix_regenerates_missing_env_from_template(tmp_path, monkeypatch):
    (tmp_path / ".env.example").write_text("API_KEY=\n")
    eng = Engine(project_dir=tmp_path)
    eng._write_state(last_launch=[{"name": "root", "cwd": str(tmp_path),
                                    "command": ["node", "server.js"], "port": 8080}])
    monkeypatch.setattr(eng, "_diagnose_stopped_containers", lambda state: [])
    monkeypatch.setattr(eng, "stop", lambda: None)
    monkeypatch.setattr(eng, "run", lambda: None)

    assert not (tmp_path / ".env").exists()
    assert eng.fix() is True
    assert (tmp_path / ".env").exists()


def test_fix_diagnoses_port_conflict_from_log(tmp_path, monkeypatch):
    import devready.engine as engine_mod

    eng = Engine(project_dir=tmp_path)
    eng._write_state(last_launch=[{"name": "root", "cwd": str(tmp_path),
                                    "command": ["node", "server.js"], "port": 3000}])
    eng._state_dir.mkdir(parents=True, exist_ok=True)
    (eng._state_dir / "last-run.log").write_text(
        "Error: listen EADDRINUSE: address already in use :::3000\n"
    )
    monkeypatch.setattr(eng, "_diagnose_stopped_containers", lambda state: [])
    monkeypatch.setattr(eng, "_port_owner_info", lambda port: "python.exe (pid 999)")
    monkeypatch.setattr(eng, "stop", lambda: None)
    monkeypatch.setattr(eng, "run", lambda: None)

    messages = []
    monkeypatch.setattr(
        engine_mod.console, "print",
        lambda *a, **k: messages.append(" ".join(str(x) for x in a)),
    )
    assert eng.fix() is True
    assert any("3000" in m and "python.exe" in m for m in messages)


def test_fix_reinstalls_on_module_error_in_log(tmp_path, monkeypatch):
    import devready.engine as engine_mod

    (tmp_path / "requirements.txt").write_text("flask\n")
    eng = Engine(project_dir=tmp_path)
    # venv looks fine (not broken) so only the log-signature path triggers.
    monkeypatch.setattr(eng, "_runtime_looks_broken", lambda det: False)
    eng._write_state(last_launch=[{"name": "root", "cwd": str(tmp_path),
                                    "command": ["python", "app.py"], "port": 5000}])
    eng._state_dir.mkdir(parents=True, exist_ok=True)
    (eng._state_dir / "last-run.log").write_text(
        "ModuleNotFoundError: No module named 'flask'\n"
    )
    calls = []
    monkeypatch.setattr(
        engine_mod.version_manager, "setup_environment",
        lambda pdir, det, healer: calls.append(det.language) or [],
    )
    monkeypatch.setattr(eng, "_diagnose_stopped_containers", lambda state: [])
    monkeypatch.setattr(eng, "stop", lambda: None)
    monkeypatch.setattr(eng, "run", lambda: None)

    assert eng.fix() is True
    assert calls == ["Python"]


def test_fix_reports_nothing_found_without_llm(tmp_path, monkeypatch):
    from devready.config import Config

    # An explicit, isolated (unconfigured) Config — this dev machine's real
    # ~/.devready/config.json may have a key set, which Engine() would
    # otherwise pick up via Config.load() and break the "no LLM" assumption.
    eng = Engine(project_dir=tmp_path, config=Config())
    eng._write_state(last_launch=[{"name": "root", "cwd": str(tmp_path),
                                    "command": ["python", "app.py"], "port": 5000}])
    monkeypatch.setattr(eng, "_diagnose_stopped_containers", lambda state: [])
    monkeypatch.setattr(eng, "stop", lambda: None)
    monkeypatch.setattr(eng, "run", lambda: None)
    assert eng.config.llm.is_configured is False

    import devready.engine as engine_mod
    messages = []
    monkeypatch.setattr(
        engine_mod.console, "print",
        lambda *a, **k: messages.append(" ".join(str(x) for x in a)),
    )
    assert eng.fix() is True
    assert any("Nothing obviously broken" in m for m in messages)


def test_fix_never_auto_runs_llm_suggested_commands(tmp_path, monkeypatch):
    # Safety invariant: fix()'s AI path is diagnosis-only. Even if the model
    # returned something command-shaped, nothing must execute it.
    eng = Engine(project_dir=tmp_path)
    eng._write_state(last_launch=[{"name": "root", "cwd": str(tmp_path),
                                    "command": ["python", "app.py"], "port": 5000}])
    eng.config.llm.provider = "openrouter"
    eng.config.llm.api_key = "test-key"
    monkeypatch.setattr(eng, "_diagnose_stopped_containers", lambda state: [])
    monkeypatch.setattr(
        eng, "_ask_runtime_diagnosis",
        lambda state: "Try running a destructive command (simulated bad model output)",
    )
    import devready.engine as engine_mod
    monkeypatch.setattr(
        engine_mod, "run_command",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("fix() must never execute a command")),
    )
    monkeypatch.setattr(eng, "stop", lambda: None)
    monkeypatch.setattr(eng, "run", lambda: None)
    assert eng.fix() is True


def test_diagnose_stopped_containers_shows_logs(tmp_path, monkeypatch):
    import devready.engine as engine_mod
    from devready.utils import CommandResult

    eng = Engine(project_dir=tmp_path)
    state = {"docker_containers": ["myapp"]}
    monkeypatch.setattr(engine_mod, "command_exists", lambda n: n == "docker")
    monkeypatch.setattr(eng, "_docker_container_exists", lambda name, env=None: True)
    monkeypatch.setattr(eng, "_docker_container_running", lambda name, env=None: False)
    monkeypatch.setattr(
        engine_mod, "run_command",
        lambda cmd, **k: CommandResult("docker logs", 0, stdout="Error: crashed on boot\n"),
    )
    result = eng._diagnose_stopped_containers(state)
    assert result == ["diagnosed the stopped container myapp"]


def test_port_owner_info_windows(monkeypatch):
    import devready.engine as engine_mod
    from devready.utils import CommandResult

    monkeypatch.setattr(engine_mod.sys, "platform", "win32")

    def fake_run(cmd, **k):
        if cmd[0] == "netstat":
            return CommandResult(
                "netstat", 0,
                stdout="  TCP    0.0.0.0:3000    0.0.0.0:0   LISTENING    4821\n",
            )
        if cmd[0] == "tasklist":
            return CommandResult("tasklist", 0, stdout='"node.exe","4821","Console","1","50,000 K"\n')
        return CommandResult("x", 1)

    monkeypatch.setattr(engine_mod, "run_command", fake_run)
    assert Engine._port_owner_info(3000) == "node.exe (pid 4821)"
