"""Tests for project-declared setup-strategy detection.

These verify we recognise a project's own setup method (Makefile, Justfile,
Taskfile, setup script) and pick a sensible target. Detection is pure/read-only,
so no commands are ever executed here.
"""

from devready.environment.strategies import detect_setup_strategies


def test_makefile_setup_target_detected(tmp_path):
    (tmp_path / "Makefile").write_text("setup:\n\tpip install -r requirements.txt\n\ntest:\n\tpytest\n")
    strategies = detect_setup_strategies(tmp_path)

    assert any(s.name == "makefile" and s.command == ["make", "setup"] for s in strategies)


def test_makefile_prefers_setup_over_install(tmp_path):
    # Both targets exist; "setup" must win over the more ambiguous "install".
    (tmp_path / "Makefile").write_text("install:\n\tpip install .\n\nsetup:\n\techo hi\n")
    strategies = detect_setup_strategies(tmp_path)
    makefile = next(s for s in strategies if s.name == "makefile")
    assert makefile.command == ["make", "setup"]


def test_setup_script_detected(tmp_path):
    (tmp_path / "setup.sh").write_text("#!/usr/bin/env bash\npip install -r requirements.txt\n")
    strategies = detect_setup_strategies(tmp_path)
    assert any(s.name == "script" and s.display == "bash setup.sh" for s in strategies)


def test_justfile_detected(tmp_path):
    (tmp_path / "Justfile").write_text("setup:\n    pip install -e .\n")
    strategies = detect_setup_strategies(tmp_path)
    assert any(s.name == "justfile" and s.command == ["just", "setup"] for s in strategies)


def test_nested_scripts_setup_detected(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "setup.sh").write_text("echo setup\n")
    strategies = detect_setup_strategies(tmp_path)
    assert any(s.command == ["bash", "scripts/setup.sh"] for s in strategies)


def test_no_strategy_for_plain_project(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests\n")
    assert detect_setup_strategies(tmp_path) == []
