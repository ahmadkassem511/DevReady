"""Generate a friendly, project-specific "how to use this" guide.

After setup, a web app gets opened in the browser — but many projects are CLIs,
libraries, build tools, or server apps with no single localhost. For those,
DevReady shouldn't just say "no web page to open"; it should read the README and
tell the user, in plain language, what the project is and exactly how to run or
use it. This module asks the LLM for that guide as structured JSON.

Falls back to ``None`` whenever the LLM isn't configured or reachable, so the
engine can degrade to its offline heuristics.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from ..config import Config
from .readme_parser import _as_str_list

GUIDE_SYSTEM_PROMPT = (
    "You are DevReady's onboarding guide. A project has just been installed on "
    "the user's machine (dependencies are set up). Using the README and the facts "
    "given, explain in plain, beginner-friendly language how to actually run or "
    "use THIS project. Return ONLY a JSON object with exactly these keys:\n"
    '  "what_it_is": 1-2 plain sentences on what the project is and does,\n'
    '  "has_web_ui": boolean — true only if it runs as a site/app opened in a browser,\n'
    '  "launch_command": the SINGLE shell command that starts the app/server '
    "(e.g. \"make dev\", \"npm start\", \"docker compose up\"), or an empty string "
    "if there isn't one. Must be one command — no '&&', pipes, or 'cd'. "
    "For a documented `docker run`, copy it COMPLETE: keep every flag exactly as "
    "the README shows (-e environment variables, every -p port, -v volumes, "
    "--shm-size, the exact image tag), flattening multi-line backslash "
    "continuations into one line. NEVER drop flags to simplify — omitting a "
    "documented -e (e.g. a password) ships a broken app.\n"
    '  "url": the local URL the app serves on once started (e.g. '
    '"http://localhost:8080"), or an empty string,\n'
    '  "server_command": if this project is primarily a long-running SERVER, '
    "gateway, daemon, or backend service you start from the command line (NOT a "
    'browser page) — the SINGLE command that starts that server (e.g. '
    '"openclaw gateway run", "myapp serve", "foo start"); else an empty string. '
    "One command only, no operators.\n"
    '  "onboarding_command": if the project needs a ONE-TIME interactive setup '
    "before it works — onboarding/login/init, e.g. to enter an API key or choose "
    'options — the SINGLE command that runs it (e.g. "openclaw onboard", '
    '"gh auth login", "foo init"); else an empty string. One command, no operators.\n'
    '  "steps": array of short, concrete, copy-pasteable steps to run or use it '
    "(include the actual commands; assume deps are already installed so do NOT "
    "include install steps). If the app has a login screen, one step MUST state "
    "the documented default username/password (or how they're set, e.g. which "
    "-e variables),\n"
    '  "tips": one short sentence on prerequisites or what to do next (e.g. needs a '
    "database/API key), or an empty string.\n"
    "Prefer the project's own documented commands. If it's a library, make the "
    "steps a minimal usage example and leave launch_command empty. Keep it concise "
    "— at most 6 steps."
)


def generate_project_guide(
    config: Config,
    project_dir: Path,
    detections,
    insights,
    *,
    served_urls: Optional[List[str]] = None,
    readme_text: str = "",
) -> Optional[dict]:
    """Ask the LLM for a project-specific usage guide. Returns a dict or None.

    The returned dict has keys: ``what_it_is`` (str), ``has_web_ui`` (bool),
    ``steps`` (list[str]), ``tips`` (str).
    """
    if not config.llm.is_configured:
        return None

    from .client import ask_llm_json

    langs = ", ".join(sorted({d.language for d in detections})) or "unknown"
    frameworks = ", ".join(sorted({f for d in detections for f in d.frameworks})) or "none"
    cmds = insights.commands[:10] if insights and insights.commands else []
    key_files = _key_files(project_dir)

    user_prompt = (
        f"Project folder name: {project_dir.name}\n"
        f"Languages: {langs}\n"
        f"Frameworks: {frameworks}\n"
        f"Key files present: {key_files}\n"
        f"Commands the README mentions: {cmds}\n"
        f"Already-running local URLs: {served_urls or 'none'}\n\n"
        f"README (excerpt):\n{(readme_text or '').strip()[:6000] or '(no README found)'}\n"
    )

    data = ask_llm_json(config, GUIDE_SYSTEM_PROMPT, user_prompt)
    if not isinstance(data, dict):
        return None

    steps = _as_str_list(data.get("steps"))
    what = str(data.get("what_it_is", "")).strip()
    if not steps and not what:
        return None  # nothing useful came back
    return {
        "what_it_is": what,
        "has_web_ui": bool(data.get("has_web_ui")),
        "launch_command": str(data.get("launch_command", "")).strip(),
        "url": str(data.get("url", "")).strip(),
        "server_command": str(data.get("server_command", "")).strip(),
        "onboarding_command": str(data.get("onboarding_command", "")).strip(),
        "steps": steps,
        "tips": str(data.get("tips", "")).strip(),
    }


# Run/build tools we'll auto-execute as a launch command. Broader than the
# healer's install allowlist, but still a closed set of build/run tools.
_SAFE_LAUNCH_HEADS = {
    "make", "just", "task", "npm", "npx", "yarn", "pnpm", "corepack", "node",
    "deno", "bun", "python", "python3", "py", "flask", "uvicorn", "gunicorn",
    "hypercorn", "streamlit", "php", "composer", "artisan", "ruby", "bundle",
    "rails", "rackup", "cargo", "go", "dotnet", "docker", "docker-compose",
    "mvn", "gradle", "gradlew", "mvnw", "air", "vite", "next", "ng", "rake",
}

_LAUNCH_FORBIDDEN = (
    "rm ", "rmdir", "del ", "format ", "mkfs", "dd ", ":(){", "shutdown",
    "reboot", "> /dev", "| sh", "| bash", "| iex", "curl ", "wget ", "iwr ",
    "irm ", "-enc", "reg delete", "deltree",
)

# Shell operators we won't auto-run (we execute a single argv, not a shell line).
_SHELL_OPERATORS = ("&&", "||", ";", "|", ">", "<", "`", "$(")


def is_safe_launch_command(command: str) -> bool:
    """Return True if a guide's ``launch_command`` is safe to auto-run.

    Accepts a single build/run command whose head is a known tool; rejects shell
    chains, redirections, and any destructive/pipe-to-shell tokens. Conservative
    by design — DevReady already runs project-defined dev commands, this just
    lets it run the *documented* one the README recommends.
    """
    if not command or not command.strip():
        return False
    low = command.lower()
    if any(op in command for op in _SHELL_OPERATORS):
        return False
    if any(tok in low for tok in _LAUNCH_FORBIDDEN):
        return False
    parts = command.split()
    head = parts[0]
    if head == "sudo" and len(parts) > 1:
        head = parts[1]
    head = head.replace("\\", "/").split("/")[-1].lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if head.endswith(suffix):
            head = head[: -len(suffix)]
    return head in _SAFE_LAUNCH_HEADS


def is_safe_server_command(command: str) -> bool:
    """Like :func:`is_safe_launch_command`, but the head may be a project-specific
    CLI (e.g. ``openclaw``) that the engine resolves to ``node``/``pnpm``/``npx``.

    We therefore skip the known-head allowlist but still reject shell chains,
    redirections, and any destructive/pipe-to-shell tokens.
    """
    if not command or not command.strip():
        return False
    low = command.lower()
    if any(op in command for op in _SHELL_OPERATORS):
        return False
    if any(tok in low for tok in _LAUNCH_FORBIDDEN):
        return False
    return True


def port_from_url(url: str) -> Optional[int]:
    """Extract the port from a URL like ``http://localhost:8080`` (else None)."""
    import re

    match = re.search(r":(\d{2,5})", url or "")
    if match:
        value = int(match.group(1))
        if 1 <= value <= 65535:
            return value
    return None


# Files that signal how a project is meant to be run, given to the LLM as context.
_GUIDE_KEY_FILES = [
    "package.json", "pyproject.toml", "setup.py", "requirements.txt", "Pipfile",
    "Makefile", "Cargo.toml", "go.mod", "Gemfile", "composer.json", "pom.xml",
    "build.gradle", "Dockerfile", "docker-compose.yml", "manage.py", "main.py",
    "app.py", "index.js", "cli.py", "__main__.py",
]


def _key_files(project_dir: Path) -> str:
    """A short list of present run-signalling files, for the LLM's context."""
    present = [name for name in _GUIDE_KEY_FILES if (project_dir / name).exists()]
    return ", ".join(present) or "unknown"
