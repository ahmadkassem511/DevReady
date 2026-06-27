"""Tests for the local web GUI: security, catalog, install-job validation, API.

These never hit the network or run a real install — we test the *guards* (token,
host/origin, URL validation) and the read-only endpoints. The actual clone/setup
path is exercised via the CLI elsewhere.
"""

import pytest

# The web layer is an optional extra; skip cleanly if it isn't installed.
pytest.importorskip("fastapi")

import devready.config as config_module
from devready.web import catalog, github, security
from devready.web.jobs import project_name_from_url, validate_repo_url


class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_build_query():
    # Featured has no topic query -> default "most-starred overall" floor.
    assert "stars:>20000" in github.build_query("", "featured")
    # A category adds its topic and a lower star floor.
    q = github.build_query("", "ai")
    assert "topic:machine-learning" in q and "stars:>500" in q
    # Free text is included.
    assert github.build_query("chat", "").startswith("chat")


def test_search_repositories_maps_and_handles_errors(monkeypatch):
    sample = {
        "items": [
            {
                "name": "cool-app", "full_name": "owner/cool-app",
                "clone_url": "https://github.com/owner/cool-app.git",
                "html_url": "https://github.com/owner/cool-app",
                "stargazers_count": 45200, "language": "Python",
                "description": "Does cool things.", "topics": ["ai", "cli"],
            }
        ]
    }
    monkeypatch.setattr(github.httpx, "get", lambda *a, **k: _FakeResponse(200, sample))
    projects, error = github.search_repositories(category="ai")
    assert error == ""
    assert projects[0]["stars"] == 45200
    assert projects[0]["repo"] == "https://github.com/owner/cool-app.git"

    # Rate limit -> friendly error, no results.
    monkeypatch.setattr(github.httpx, "get", lambda *a, **k: _FakeResponse(403))
    projects, error = github.search_repositories()
    assert projects == [] and "limit" in error.lower()


def test_search_results_are_cached(monkeypatch):
    github._CACHE.clear()
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return _FakeResponse(200, {"items": [{"name": "a", "full_name": "o/a", "stargazers_count": 1}]})

    monkeypatch.setattr(github.httpx, "get", fake_get)
    github.search_repositories(category="ai")
    github.search_repositories(category="ai")  # identical query -> served from cache
    assert calls["n"] == 1


def _redirect_home(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


# -- security ---------------------------------------------------------------
def test_token_matches():
    assert security.token_matches("abc", "abc") is True
    assert security.token_matches("abc", "xyz") is False
    assert security.token_matches("abc", None) is False
    assert security.token_matches("abc", "") is False


def test_host_allowed():
    assert security.host_is_allowed("127.0.0.1:8765") is True
    assert security.host_is_allowed("localhost:9000") is True
    assert security.host_is_allowed("evil.com") is False
    assert security.host_is_allowed(None) is False


def test_origin_allowed():
    assert security.origin_is_allowed(None) is True  # absent is fine
    assert security.origin_is_allowed("http://127.0.0.1:8765") is True
    assert security.origin_is_allowed("http://evil.com") is False


# -- catalog ----------------------------------------------------------------
def test_catalog_loads_and_searches():
    assert len(catalog.all_projects()) > 0
    assert len(catalog.categories()) > 0
    # Searching by a tag/word present in the seed catalog returns something.
    ai_hits = catalog.search(category="ai")
    assert all(p["category"] == "ai" for p in ai_hits)
    assert catalog.search(query="zzz-nonexistent-zzz") == []


def test_is_known_repo():
    known = catalog.all_projects()[0]["repo"]
    assert catalog.is_known_repo(known) is True
    assert catalog.is_known_repo("https://github.com/random/unknown") is False


# -- install job URL validation --------------------------------------------
def test_validate_repo_url_accepts_github():
    assert validate_repo_url("https://github.com/owner/repo") == "https://github.com/owner/repo"
    assert validate_repo_url("https://github.com/owner/repo/") == "https://github.com/owner/repo"


def test_validate_repo_url_rejects_bad():
    with pytest.raises(ValueError):
        validate_repo_url("http://evil.com/x")  # disallowed host
    with pytest.raises(ValueError):
        validate_repo_url("ftp://github.com/x")  # disallowed scheme
    with pytest.raises(ValueError):
        validate_repo_url("not a url")


def test_project_name_from_url():
    assert project_name_from_url("https://github.com/owner/My-Repo.git") == "My-Repo"
    assert project_name_from_url("https://github.com/owner/cool-tool") == "cool-tool"


def test_gui_install_uses_the_same_cli_path(tmp_path, monkeypatch):
    """The GUI must install via `devready start --yes`, not its own logic.

    This is what guarantees the GUI gets the *same* per-project isolation as the
    CLI (correct Python version + dedicated .venv). We capture the commands the
    job runs and assert the setup step is exactly the CLI entry point.
    """
    import sys

    import devready.web.jobs as jobs_module

    monkeypatch.setattr(jobs_module.Path, "home", lambda: tmp_path)

    recorded = []

    def fake_stream(self, command, job):
        recorded.append(command)
        return 0  # pretend clone + setup both succeeded

    monkeypatch.setattr(jobs_module.JobManager, "_stream", fake_stream)

    mgr = jobs_module.JobManager()
    job = mgr.start_install("https://github.com/owner/repo")
    # Drain the queue until the job thread finishes.
    while True:
        if job.queue.get(timeout=5) is jobs_module._DONE:
            break

    setup_cmd = recorded[-1]  # last command is the setup invocation
    assert setup_cmd[:4] == [sys.executable, "-m", "devready", "start"]
    assert "--yes" in setup_cmd


# -- API endpoints (with the security middleware in force) ------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    _redirect_home(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient

    from devready.web.server import create_app

    app = create_app(token="testtoken")
    # base_url sets a loopback Host header so the security middleware is happy.
    return TestClient(app, base_url="http://127.0.0.1")


def test_api_requires_token(client):
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", headers={"X-DevReady-Token": "wrong"}).status_code == 401
    assert client.get("/api/state", headers={"X-DevReady-Token": "testtoken"}).status_code == 200


def test_api_rejects_bad_host(client):
    resp = client.get(
        "/api/state",
        headers={"X-DevReady-Token": "testtoken", "Host": "evil.com"},
    )
    assert resp.status_code == 403


def test_api_rejects_foreign_origin(client):
    resp = client.get(
        "/api/state",
        headers={"X-DevReady-Token": "testtoken", "Origin": "http://evil.com"},
    )
    assert resp.status_code == 403


def test_install_docker_endpoint_opens_page_when_no_pkg_manager(client, monkeypatch):
    # Force the "open download page" path so the test never launches a real install.
    import devready.web.server as srv

    monkeypatch.setattr(srv.shutil, "which", lambda n: None)
    opened = {}
    monkeypatch.setattr(srv.webbrowser, "open", lambda u: opened.setdefault("url", u))

    resp = client.post("/api/install-docker", headers={"X-DevReady-Token": "testtoken"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "open"
    assert "docker.com" in body["url"]
    assert opened["url"].startswith("https://")


def test_jobs_reads_needs_docker_flag(tmp_path):
    from devready.web.jobs import JobManager

    proj = tmp_path / "proj"
    (proj / ".devready").mkdir(parents=True)
    (proj / ".devready" / "state.json").write_text('{"needs_container_engine": true}')
    mgr = JobManager()
    assert mgr._read_needs_docker(proj) is True
    assert mgr._read_needs_docker(tmp_path / "missing") is False


def test_catalog_endpoint(client):
    resp = client.get("/api/catalog?category=ai", headers={"X-DevReady-Token": "testtoken"})
    assert resp.status_code == 200
    assert all(p["category"] == "ai" for p in resp.json()["projects"])


def test_discover_endpoint(client, monkeypatch):
    # Don't hit the network — stub the GitHub search.
    monkeypatch.setattr(
        github, "search_repositories",
        lambda **kw: ([{"name": "x", "stars": 100, "repo": "https://github.com/a/x.git"}], ""),
    )
    resp = client.get("/api/discover?category=ai", headers={"X-DevReady-Token": "testtoken"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["projects"][0]["stars"] == 100
    assert body["error"] == ""


def test_state_exposes_discover_categories(client):
    cats = client.get("/api/state", headers={"X-DevReady-Token": "testtoken"}).json()["categories"]
    ids = [c["id"] for c in cats]
    assert ids[0] == "featured"  # curated picks first
    assert "ai" in ids


def test_install_rejects_bad_url(client):
    resp = client.post(
        "/api/install",
        headers={"X-DevReady-Token": "testtoken"},
        json={"repo_url": "http://evil.com/x"},
    )
    assert resp.status_code == 400


def test_key_set_and_clear(client):
    h = {"X-DevReady-Token": "testtoken"}
    assert client.get("/api/state", headers=h).json()["ai_configured"] is False
    assert client.post("/api/key", headers=h, json={"api_key": "sk-or-test"}).status_code == 200
    assert client.get("/api/state", headers=h).json()["ai_configured"] is True
    assert client.delete("/api/key", headers=h).status_code == 200
    assert client.get("/api/state", headers=h).json()["ai_configured"] is False


def test_gui_relaunch_uses_devready_run(tmp_path, monkeypatch):
    """The GUI 'Run' button must relaunch via `devready run` (the CLI path)."""
    import sys

    import devready.web.jobs as jobs_module

    recorded = []

    def fake_stream(self, command, job):
        recorded.append(command)
        return 0

    monkeypatch.setattr(jobs_module.JobManager, "_stream", fake_stream)
    mgr = jobs_module.JobManager()
    job = mgr.start_relaunch(str(tmp_path / "proj"))
    while True:
        if job.queue.get(timeout=5) is jobs_module._DONE:
            break
    assert recorded[-1][:4] == [sys.executable, "-m", "devready", "run"]


def test_delete_project_unregisters(client, tmp_path):
    from devready.config import list_projects, register_project

    proj = tmp_path / "old-proj"
    proj.mkdir()
    register_project(proj)
    h = {"X-DevReady-Token": "testtoken"}
    resp = client.request("DELETE", "/api/projects", headers=h, json={"path": str(proj), "delete_files": False})
    assert resp.status_code == 200
    assert str(proj.resolve()) not in [p["path"] for p in list_projects()]
    assert proj.exists()  # files kept when delete_files is False


def test_delete_refuses_files_outside_workspace(client, tmp_path):
    # delete_files must never rmtree a path outside the DevReady workspace.
    outside = tmp_path / "outside-project"
    outside.mkdir()
    (outside / "important.txt").write_text("keep me")
    h = {"X-DevReady-Token": "testtoken"}
    resp = client.request("DELETE", "/api/projects", headers=h, json={"path": str(outside), "delete_files": True})
    assert resp.status_code == 400
    assert outside.exists()  # not deleted


def test_key_rejects_non_openrouter_key(client):
    # The GUI must reject an OpenAI key (sk-proj-...) with a helpful 400, and not
    # save it — this is the exact mistake that caused a silent 401 mid-setup.
    h = {"X-DevReady-Token": "testtoken"}
    resp = client.post("/api/key", headers=h, json={"api_key": "sk-proj-abcdef123"})
    assert resp.status_code == 400
    assert "sk-or-" in resp.json()["detail"]
    # Nothing was saved.
    assert client.get("/api/state", headers=h).json()["ai_configured"] is False
