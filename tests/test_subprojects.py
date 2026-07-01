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


def test_root_is_js_workspace_pnpm(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "root"}')
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'ui'\n")
    assert Engine(project_dir=tmp_path)._root_is_js_workspace() is True


def test_root_is_js_workspace_npm_yarn(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "root", "workspaces": ["ui", "packages/*"]}')
    assert Engine(project_dir=tmp_path)._root_is_js_workspace() is True


def test_root_not_js_workspace(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "solo", "dependencies": {"react": "18"}}')
    assert Engine(project_dir=tmp_path)._root_is_js_workspace() is False


def test_workspace_member_node_install_is_skipped(tmp_path, monkeypatch):
    # openclaw case: root is a pnpm workspace; a ui/ member uses workspace: deps.
    # DevReady must NOT run a separate install in ui/ (npm can't parse it), since
    # the root install already covered it.
    import devready.engine as engine_mod

    (tmp_path / "package.json").write_text('{"name": "root"}')
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'ui'\n")
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "package.json").write_text('{"name": "ui", "dependencies": {"shared": "workspace:*"}}')

    eng = Engine(project_dir=tmp_path)
    eng.assume_yes = True
    called = {"setup": 0}
    monkeypatch.setattr(
        engine_mod.version_manager, "setup_environment",
        lambda *a, **k: called.__setitem__("setup", called["setup"] + 1) or [],
    )
    eng._setup_subprojects()
    assert called["setup"] == 0  # the workspace member's install was skipped
