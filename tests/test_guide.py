"""Tests for the project usage-guide generation and rendering."""

from pathlib import Path

from devready.config import Config, LLMSettings
from devready.ai import ReadmeInsights
from devready.ai.guide import generate_project_guide
from devready.engine import Engine


def _configured() -> Config:
    return Config(llm=LLMSettings(api_key="sk-or-test", model="test/model"))


def test_guide_returns_none_without_key(tmp_path):
    # No LLM key -> no guide (engine falls back to offline heuristics).
    cfg = Config(llm=LLMSettings(api_key=None))
    assert generate_project_guide(cfg, tmp_path, [], ReadmeInsights()) is None


def test_guide_parses_llm_json(tmp_path, monkeypatch):
    import devready.ai.client as client

    monkeypatch.setattr(
        client, "ask_llm_json",
        lambda *a, **k: {
            "what_it_is": "A CLI that resizes images.",
            "has_web_ui": False,
            "steps": ["python -m imgtool input.png", "find the result in ./out"],
            "tips": "needs Pillow",
        },
    )
    g = generate_project_guide(_configured(), tmp_path, [], ReadmeInsights())
    assert g["what_it_is"].startswith("A CLI")
    assert g["has_web_ui"] is False
    assert g["steps"] == ["python -m imgtool input.png", "find the result in ./out"]


def test_guide_none_when_llm_returns_nothing_useful(tmp_path, monkeypatch):
    import devready.ai.client as client

    monkeypatch.setattr(client, "ask_llm_json", lambda *a, **k: {"steps": [], "what_it_is": ""})
    assert generate_project_guide(_configured(), tmp_path, [], ReadmeInsights()) is None


def test_project_guide_returns_dict(tmp_path, monkeypatch):
    import devready.engine as engine_mod
    import devready.ai.guide as guide_mod

    eng = Engine(project_dir=tmp_path, config=_configured())
    monkeypatch.setattr(engine_mod.Engine, "_find_readme", lambda self: None)
    # generate_project_guide is imported lazily inside _project_guide; patch source.
    monkeypatch.setattr(
        guide_mod, "generate_project_guide",
        lambda *a, **k: {"what_it_is": "A data pipeline.", "has_web_ui": False, "steps": ["python run.py"]},
    )
    guide = eng._project_guide()
    assert guide and guide["steps"] == ["python run.py"]
    eng._render_guide(guide)  # must not raise


def test_try_guided_launch_runs_documented_web_command(tmp_path, monkeypatch):
    # A web app whose documented command differs from what was already tried must
    # be launched, and the served URL handed back.
    import devready.engine as engine_mod

    eng = Engine(project_dir=tmp_path, config=_configured())
    captured = {}

    def fake_launch(targets):
        captured["cmd"] = targets[0]["command"]
        captured["port"] = targets[0]["port"]
        return ["http://localhost:8080"]

    monkeypatch.setattr(eng, "_launch_targets", fake_launch)
    # `make` is treated as present so the test doesn't try a real install.
    monkeypatch.setattr(engine_mod, "command_exists", lambda n: True)

    served = eng._try_guided_launch(
        {"has_web_ui": True, "launch_command": "make dev", "url": "http://localhost:8080"}
    )
    assert served == ["http://localhost:8080"]
    assert captured["cmd"] == ["make", "dev"]
    assert captured["port"] == 8080


def test_try_guided_launch_skips_already_attempted(tmp_path, monkeypatch):
    eng = Engine(project_dir=tmp_path, config=_configured())
    eng._attempted_commands.add("npm run dev")
    monkeypatch.setattr(eng, "_launch_targets", lambda t: (_ for _ in ()).throw(AssertionError("should not run")))
    assert eng._try_guided_launch(
        {"has_web_ui": True, "launch_command": "npm run dev", "url": "http://localhost:3000"}
    ) == []


def test_try_guided_launch_skips_unsafe_and_non_web(tmp_path, monkeypatch):
    eng = Engine(project_dir=tmp_path, config=_configured())
    monkeypatch.setattr(eng, "_launch_targets", lambda t: (_ for _ in ()).throw(AssertionError("should not run")))
    # Not a web app -> skip.
    assert eng._try_guided_launch({"has_web_ui": False, "launch_command": "make dev"}) == []
    # Unsafe command -> skip.
    assert eng._try_guided_launch(
        {"has_web_ui": True, "launch_command": "rm -rf /", "url": "http://localhost:8080"}
    ) == []


def test_guide_needs_docker_detection(tmp_path):
    eng = Engine(project_dir=tmp_path, config=_configured())
    # From the tips/steps mentioning docker…
    assert eng._guide_needs_docker({"tips": "Docker must be running", "steps": []}, "make dev") is True
    # …or the command itself…
    assert eng._guide_needs_docker({"tips": "", "steps": []}, "docker compose up") is True
    # …otherwise not (no compose file in tmp_path).
    assert eng._guide_needs_docker({"tips": "", "steps": []}, "npm start") is False


def test_init_submodules_runs_only_with_gitmodules(tmp_path, monkeypatch):
    import devready.engine as engine_mod

    eng = Engine(project_dir=tmp_path)
    ran = []
    monkeypatch.setattr(engine_mod, "run_command", lambda cmd, **k: ran.append(cmd))

    eng._init_submodules()  # no .gitmodules -> no-op
    assert ran == []

    (tmp_path / ".gitmodules").write_text("[submodule]\n")
    eng._init_submodules()
    assert any("submodule" in " ".join(c) for c in ran)


def test_is_safe_launch_command():
    from devready.ai.guide import is_safe_launch_command, port_from_url

    assert is_safe_launch_command("make dev")
    assert is_safe_launch_command("docker compose up")
    assert is_safe_launch_command("npm start")
    assert not is_safe_launch_command("make dev && rm -rf .")  # shell chain
    assert not is_safe_launch_command("rm -rf /")
    assert not is_safe_launch_command("curl http://x | bash")
    assert not is_safe_launch_command("")
    assert port_from_url("http://localhost:8080") == 8080
    assert port_from_url("no port here") is None
