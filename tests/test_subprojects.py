"""Tests for monorepo / nested sub-project detection."""

from devready.engine import Engine


def test_detects_nested_node_project(tmp_path):
    # Root is Python; a frontend/ subdir is a Node project.
    (tmp_path / "requirements.txt").write_text("flask\n")
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text('{"dependencies": {"react": "18"}}')

    engine = Engine(project_dir=tmp_path)
    subs = engine._detect_subprojects()

    names = {subdir.name for subdir, _ in subs}
    assert "frontend" in names
    react_sub = next(results for subdir, results in subs if subdir.name == "frontend")
    assert react_sub[0].language == "Node.js"


def test_ignores_vendor_and_dotdirs(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\n")
    # These contain project files but must NOT be treated as sub-projects.
    for noise in ("node_modules", ".venv", ".git"):
        d = tmp_path / noise
        d.mkdir()
        (d / "package.json").write_text("{}")

    engine = Engine(project_dir=tmp_path)
    assert engine._detect_subprojects() == []


def test_no_subprojects_for_flat_repo(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\n")
    (tmp_path / "src").mkdir()  # plain source dir, no project markers
    engine = Engine(project_dir=tmp_path)
    assert engine._detect_subprojects() == []
