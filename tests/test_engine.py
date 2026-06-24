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
