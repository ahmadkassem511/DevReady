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


def test_print_project_guide_shows_and_returns_true(tmp_path, monkeypatch, capsys):
    import devready.engine as engine_mod

    eng = Engine(project_dir=tmp_path, config=_configured())
    monkeypatch.setattr(
        engine_mod.Engine, "_find_readme", lambda self: None
    )
    # Stub the LLM call so the test is offline/deterministic.
    import devready.ai.guide as guide_mod

    monkeypatch.setattr(
        guide_mod, "generate_project_guide",
        lambda *a, **k: {
            "what_it_is": "A data pipeline.",
            "has_web_ui": False,
            "steps": ["python run.py"],
            "tips": "",
        },
    )
    assert eng._print_project_guide() is True


def test_print_project_guide_false_without_guide(tmp_path, monkeypatch):
    import devready.ai.guide as guide_mod

    eng = Engine(project_dir=tmp_path, config=Config(llm=LLMSettings(api_key=None)))
    monkeypatch.setattr(guide_mod, "generate_project_guide", lambda *a, **k: None)
    assert eng._print_project_guide() is False
