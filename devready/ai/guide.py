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
    '  "steps": array of short, concrete, copy-pasteable steps to run or use it '
    "(include the actual commands; assume deps are already installed so do NOT "
    "include install steps),\n"
    '  "tips": one short sentence on prerequisites or what to do next (e.g. needs a '
    "database/API key), or an empty string.\n"
    "Prefer the project's own documented commands. If it's a library, make the "
    "steps a minimal usage example. Keep it concise — at most 6 steps."
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
        "steps": steps,
        "tips": str(data.get("tips", "")).strip(),
    }


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
