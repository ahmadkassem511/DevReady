"""Tests for the stack detectors.

These create throwaway project directories with fixture files and assert that
the right detector fires with the expected version/frameworks. The ``tmp_path``
fixture is provided by pytest and gives each test its own temp directory.
"""

from devready.detectors import detect_stack
from devready.detectors.node import NodeDetector
from devready.detectors.python import PythonDetector


def test_python_detected_from_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("django==4.2\ncelery\n")
    result = PythonDetector(tmp_path).detect()

    assert result is not None
    assert result.language == "Python"
    assert "Django" in result.frameworks
    assert "Celery" in result.frameworks
    assert "requirements.txt" in result.package_files


def test_python_version_from_python_version_file(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\n")
    (tmp_path / ".python-version").write_text("3.11.4\n")
    result = PythonDetector(tmp_path).detect()

    assert result is not None
    assert result.version == "3.11.4"


def test_python_version_from_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.10"\n')
    result = PythonDetector(tmp_path).detect()

    assert result is not None
    assert result.version == "3.10"


def test_node_detected_with_frameworks_and_version(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"next": "14", "react": "18"}, "engines": {"node": ">=20"}}'
    )
    result = NodeDetector(tmp_path).detect()

    assert result is not None
    assert result.language == "Node.js"
    assert "Next.js" in result.frameworks
    assert "React" in result.frameworks
    assert result.version == "20"


def test_node_version_from_nvmrc_wins(tmp_path):
    (tmp_path / "package.json").write_text('{"engines": {"node": ">=18"}}')
    (tmp_path / ".nvmrc").write_text("v20.10.0\n")
    result = NodeDetector(tmp_path).detect()

    assert result is not None
    # .nvmrc is more authoritative than the engines range.
    assert result.version == "20.10.0"


def test_unknown_project_returns_empty(tmp_path):
    (tmp_path / "random.txt").write_text("nothing to see")
    assert detect_stack(tmp_path) == []


def test_polyglot_repo_detects_both(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "package.json").write_text('{"dependencies": {"vue": "3"}}')
    results = detect_stack(tmp_path)

    languages = {r.language for r in results}
    assert languages == {"Python", "Node.js"}
