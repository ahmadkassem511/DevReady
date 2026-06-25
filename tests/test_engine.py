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
