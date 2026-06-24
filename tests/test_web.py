"""Tests for the local web GUI: security, catalog, install-job validation, API.

These never hit the network or run a real install — we test the *guards* (token,
host/origin, URL validation) and the read-only endpoints. The actual clone/setup
path is exercised via the CLI elsewhere.
"""

import pytest

# The web layer is an optional extra; skip cleanly if it isn't installed.
pytest.importorskip("fastapi")

import devready.config as config_module
from devready.web import catalog, security
from devready.web.jobs import project_name_from_url, validate_repo_url


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


def test_catalog_endpoint(client):
    resp = client.get("/api/catalog?category=ai", headers={"X-DevReady-Token": "testtoken"})
    assert resp.status_code == 200
    assert all(p["category"] == "ai" for p in resp.json()["projects"])


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


def test_key_rejects_non_openrouter_key(client):
    # The GUI must reject an OpenAI key (sk-proj-...) with a helpful 400, and not
    # save it — this is the exact mistake that caused a silent 401 mid-setup.
    h = {"X-DevReady-Token": "testtoken"}
    resp = client.post("/api/key", headers=h, json={"api_key": "sk-proj-abcdef123"})
    assert resp.status_code == 400
    assert "sk-or-" in resp.json()["detail"]
    # Nothing was saved.
    assert client.get("/api/state", headers=h).json()["ai_configured"] is False
