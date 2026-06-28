"""The local web server behind ``devready ui``.

Builds a FastAPI app that serves the browser GUI and a small JSON API. The app
drives the same :class:`devready.engine.Engine` the CLI uses, so the GUI is just
a friendlier front door — not a separate codebase.

Everything here is local-only and gated by :mod:`devready.web.security`:
the security middleware runs before every handler and enforces loopback Host,
same-origin, and the per-launch token on ``/api`` routes.
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Optional

# FastAPI is imported at module level (not lazily inside functions) so that
# FastAPI can resolve the request/response type hints — with `from __future__
# import annotations`, hints are strings and must be resolvable from module
# globals. This module is only ever imported on demand (the `ui` CLI command
# probes for FastAPI first, and the tests skip when it's absent), so the core
# CLI still doesn't require FastAPI to be installed.
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from ..config import Config, list_projects
from . import catalog, github, security
from .jobs import _DONE, JobManager

_STATIC_DIR = Path(__file__).with_name("static")


def _run_check(repo_url: str):
    """Clone a repo, detect its stack, read its README, and check hardware.

    Runs in a thread (via ``asyncio.to_thread``) so the async endpoint doesn't
    block. The cloned directory is always cleaned up before returning.

    Returns a ``system_check.CompatibilityReport`` or raises ``HTTPException``.
    """
    import tempfile

    from ..environment import system_check
    from ..engine import Engine as _Engine

    tmp = Path(tempfile.mkdtemp())
    try:
        target = tmp / "repo"
        result = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(target)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "Clone failed").strip()
            raise HTTPException(status_code=400, detail=detail)

        config = Config.load()
        engine = _Engine(project_dir=target, config=config, assume_yes=True)
        engine._step_detect()
        engine._step_analyze_readme()

        readme = engine._find_readme()
        readme_text = readme.read_text(encoding="utf-8") if readme else ""
        hw = system_check.get_hardware_info(target)
        req = system_check.extract_requirements(readme_text, config, engine.detections)
        report = system_check.check_compatibility(hw, req)
        return report
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def create_app(token: Optional[str] = None, job_manager: Optional[JobManager] = None) -> FastAPI:
    """Build and return the FastAPI app.

    ``token`` defaults to a fresh random token; tests pass a known one. The
    returned app stores both the token and the job manager on ``app.state``.
    """
    app = FastAPI(title="DevReady", docs_url=None, redoc_url=None)
    app.state.token = token or security.generate_token()
    app.state.jobs = job_manager or JobManager()

    # -- Security middleware: runs before every request -------------------
    class SecurityMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            # 1) Host must be loopback (blocks DNS-rebinding via a foreign host).
            if not security.host_is_allowed(request.headers.get("host")):
                return JSONResponse({"detail": "Bad host"}, status_code=403)
            # 2) A present Origin must be same-origin (blocks cross-site scripting our API).
            if not security.origin_is_allowed(request.headers.get("origin")):
                return JSONResponse({"detail": "Bad origin"}, status_code=403)
            # 3) /api routes require the per-launch token (header or ?token=).
            if request.url.path.startswith("/api"):
                provided = request.headers.get("x-devready-token") or request.query_params.get("token")
                if not security.token_matches(app.state.token, provided):
                    return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            return await call_next(request)

    app.add_middleware(SecurityMiddleware)

    # -- Pages -------------------------------------------------------------
    @app.get("/")
    def index():
        return FileResponse(_STATIC_DIR / "index.html")

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # -- API: configuration & catalog ------------------------------------
    @app.get("/api/state")
    def get_state():
        """What the GUI needs on load: AI-key status + Discover categories."""
        config = Config.load()
        return {
            "ai_configured": config.llm.is_configured,
            "model": config.llm.model,
            "github_configured": bool(config.github_token),
            "categories": github.DISCOVER_CATEGORIES,
        }

    @app.get("/api/catalog")
    def get_catalog(q: str = "", category: str = ""):
        """The curated, vetted picks ('Featured' tab) — safe by default."""
        return {"projects": catalog.search(q, category)}

    @app.get("/api/discover")
    def discover(q: str = "", category: str = "", page: int = 1):
        """Browse the most-starred public repos on GitHub by topic/search."""
        token = Config.load().github_token
        projects, error = github.search_repositories(text=q, category=category, page=page, token=token)
        return {"projects": projects, "error": error, "page": page}

    @app.post("/api/github-token")
    async def set_github_token(request: Request):
        """Store an optional GitHub token to raise the Discover search limit."""
        body = await request.json()
        Config.load().set_github_token((body.get("token") or "").strip() or None)
        return {"github_configured": bool(Config.load().github_token)}

    @app.delete("/api/github-token")
    def clear_github_token():
        Config.load().set_github_token(None)
        return {"github_configured": False}

    @app.post("/api/explain")
    async def explain(request: Request):
        """Rewrite a project's description in plain language using the free LLM.

        Optional nicety for non-technical users. Falls back to the original text
        when no (valid) AI key is configured, so it never blocks.
        """
        body = await request.json()
        name = (body.get("name") or "").strip()
        description = (body.get("description") or "").strip()
        config = Config.load()
        if not config.llm.is_configured:
            return {"text": description, "ai": False}
        simple = _explain_simply(config, name, description)
        return {"text": simple or description, "ai": bool(simple)}

    @app.post("/api/key")
    async def set_key(request: Request):
        """Store the OpenRouter API key locally (0600), never logged or echoed."""
        body = await request.json()
        api_key = (body.get("api_key") or "").strip()
        model = (body.get("model") or "").strip() or None
        if not api_key:
            raise HTTPException(status_code=400, detail="An API key is required.")
        # Catch the common "OpenAI key pasted instead of OpenRouter" mistake
        # before it silently 401s mid-setup.
        from ..config import openrouter_key_warning

        warning = openrouter_key_warning(api_key)
        if warning:
            raise HTTPException(status_code=400, detail=warning)
        config = Config.load()
        config.set_llm("openrouter", api_key=api_key, model=model)
        return {"ai_configured": True}

    @app.delete("/api/key")
    def clear_key():
        config = Config.load()
        config.set_llm("openrouter", api_key="", model=config.llm.model)
        return {"ai_configured": False}

    # -- API: installed projects (the "Library") --------------------------
    @app.get("/api/projects")
    def get_projects():
        from ..engine import Engine, _pid_alive

        out = []
        for entry in list_projects():
            path = Path(entry.get("path", ""))
            if not path.exists():
                out.append({"path": str(path), "name": path.name, "status": "missing", "urls": []})
                continue
            engine = Engine(project_dir=path)
            procs = engine._state_processes(engine._read_state())
            running = [p for p in procs if p.get("pid") and _pid_alive(p["pid"])]
            ports = [p["port"] for p in (running or procs) if p.get("port")]
            out.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "status": "running" if running else "stopped",
                    "urls": [f"http://localhost:{port}" for port in ports],
                }
            )
        return {"projects": out}

    # -- API: project control panel (start / stop / delete) --------------
    @app.post("/api/projects/start")
    async def start_project(request: Request):
        body = await request.json()
        path = (body.get("path") or "").strip()
        if not path or not Path(path).exists():
            raise HTTPException(status_code=404, detail="Project folder not found.")
        job = app.state.jobs.start_relaunch(path)
        return {"job_id": job.id, "name": job.name}

    @app.post("/api/projects/stop")
    async def stop_project(request: Request):
        from ..engine import Engine

        body = await request.json()
        path = (body.get("path") or "").strip()
        Engine(project_dir=Path(path)).stop()
        return {"ok": True}

    @app.delete("/api/projects")
    async def delete_project(request: Request):
        """Remove a project from 'My Projects'; optionally delete its folder.

        File deletion is only allowed inside the DevReady workspace, so we can
        never rmtree an arbitrary path the registry happens to contain.
        """
        import shutil
        import stat

        from ..config import unregister_project
        from ..engine import Engine
        from .jobs import workspace_dir

        def _rmtree_error(func, path, exc_info):
            """Retry removing a file with relaxed permissions — Windows often
            marks files under node_modules as read-only, which blocks rmtree."""
            try:
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except Exception:
                pass  # will be detected by the exists() check below

        body = await request.json()
        path = (body.get("path") or "").strip()
        delete_files = bool(body.get("delete_files"))
        if not path:
            raise HTTPException(status_code=400, detail="A project path is required.")

        target = Path(path)
        if delete_files and target.exists():
            # Safety: only delete within the workspace dir.
            try:
                target.resolve().relative_to(workspace_dir().resolve())
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail="Refusing to delete files outside the DevReady workspace.",
                )
            try:
                Engine(project_dir=target).stop()  # stop anything running first
            except Exception:
                pass
            # Windows: rmtree often fails on node_modules (long paths, symlinks).
            # Use the longest possible path prefix to avoid MAX_PATH issues.
            shutil.rmtree(target, ignore_errors=False, onerror=_rmtree_error)
            if target.exists():
                # Some files couldn't be deleted — warn the user and leave the
                # registry entry so they can retry or delete manually.
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Could not fully delete {target}. Some files may be in use "
                        "or have protected permissions. Delete the folder manually "
                        "and try again."
                    ),
                )

        unregister_project(target)
        return {"ok": True}

    # -- API: compatibility check (before install) -------------------------
    @app.post("/api/check-compatibility")
    async def check_compatibility(request: Request):
        """Clone the repo, detect stack, analyse README, and check hardware.

        Returns a JSON report so the GUI can show results and offer a
        "Continue Anyway" fallback when the user's system doesn't match
        the project's requirements.
        """
        import asyncio
        from dataclasses import asdict

        body = await request.json()
        repo_url = (body.get("repo_url") or "").strip()
        if not repo_url:
            raise HTTPException(status_code=400, detail="repo_url is required")

        # Run the heavy work in a thread so the async event loop stays responsive.
        report = await asyncio.to_thread(_run_check, repo_url)

        def _check_to_dict(c):
            return {f: getattr(c, f) for f in ("name", "status", "current", "required", "message")}

        return {
            "compatible": report.compatible,
            "has_errors": report.has_errors,
            "checks": [_check_to_dict(c) for c in report.checks],
            "hw": asdict(report.hw) if report.hw else None,
            "req": asdict(report.req) if report.req else None,
        }

    # -- API: install (start a job + stream its log) ----------------------
    @app.post("/api/install")
    async def install(request: Request):
        body = await request.json()
        repo_url = (body.get("repo_url") or "").strip()
        try:
            job = app.state.jobs.start_install(repo_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"job_id": job.id, "name": job.name, "known": catalog.is_known_repo(repo_url)}

    @app.post("/api/install-docker")
    def install_docker():
        """Kick off a Docker Desktop install (one UAC click) or open its download.

        On Windows with winget we launch the install in a new console so the user
        sees progress and approves the elevation prompt; otherwise we open the
        download page. Either way Docker needs a one-time restart, so we tell the
        user to restart and click Run again.
        """
        url = "https://www.docker.com/products/docker-desktop"
        if sys.platform == "win32" and shutil.which("winget"):
            try:
                subprocess.Popen(
                    ["winget", "install", "-e", "--id", "Docker.DockerDesktop",
                     "--accept-package-agreements", "--accept-source-agreements"],
                    creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                )
                return {
                    "status": "installing",
                    "message": "Installing Docker Desktop in a new window — approve the Windows "
                               "prompt. When it finishes, RESTART your PC, then click Run again.",
                }
            except OSError:
                pass
        elif sys.platform == "darwin" and shutil.which("brew"):
            try:
                subprocess.Popen(
                    ["brew", "install", "--cask", "docker"],
                    creationflags=0,
                )
                return {
                    "status": "installing",
                    "message": "Installing Docker Desktop via Homebrew — then open Docker once "
                               "and click Run again.",
                }
            except OSError:
                pass
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return {
            "status": "open",
            "url": url,
            "message": "Opened the Docker Desktop download page. Install it, restart, then click Run again.",
        }

    @app.get("/api/jobs/{job_id}/stream")
    def job_stream(job_id: str):
        job = app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job")

        def event_stream():
            while True:
                try:
                    item = job.queue.get(timeout=1.0)
                except queue.Empty:
                    yield ": keep-alive\n\n"  # comment frame keeps the connection open
                    continue
                if item is _DONE:
                    payload = {
                        "type": "done",
                        "status": job.status,
                        "project_dir": job.project_dir,
                        "urls": job.urls,
                        "needs_docker": job.needs_docker,
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                    break
                yield f"data: {json.dumps({'type': 'log', 'line': item})}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app


def _explain_simply(config, name: str, description: str) -> str:
    """Ask the free LLM to rephrase a project's description in plain language.

    Returns a short, jargon-free sentence, or "" on any failure (so the caller
    falls back to the original description and the GUI never breaks).
    """
    import httpx

    from ..ai.readme_parser import OPENROUTER_URL

    prompt = (
        f"In one short, friendly sentence a non-technical person can understand, "
        f"explain what the project '{name}' is for. Avoid jargon. "
        f"Here is its official description: {description or name}"
    )
    headers = {
        "Authorization": f"Bearer {config.llm.api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/ahmadkassem511/DevReady",
        "X-Title": "DevReady",
    }
    payload = {
        "model": config.llm.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }
    try:
        resp = httpx.post(OPENROUTER_URL, json=payload, headers=headers, timeout=30)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        pass
    return ""


def serve(host: str = "127.0.0.1", port: int = 0, open_browser: bool = True) -> None:
    """Launch the web GUI: pick a port, open the browser with the token, run.

    ``port=0`` lets the OS pick a free port — we read it back and open the
    browser at the exact URL (including the session token).
    """
    import socket

    import uvicorn  # local import: only needed when actually serving

    token = security.generate_token()
    app = create_app(token=token)

    # Reserve a concrete port up front so we can print/open the real URL.
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            port = sock.getsockname()[1]

    url = f"http://{host}:{port}/?token={token}"

    from ..utils import console

    console.print("\n[success]DevReady is ready.[/success] Open this in your browser:")
    console.print(f"  [bold]{url}[/bold]\n")
    console.print("[muted]Keep this window open while you use DevReady. Press Ctrl+C to stop.[/muted]")

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass  # headless / no browser — the printed URL still works

    uvicorn.run(app, host=host, port=port, log_level="warning")
