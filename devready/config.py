"""Persistent configuration for DevReady.

DevReady stores its settings in ``~/.devready/config.json`` so that the user's
LLM provider and API key survive across runs and across projects.

Example file::

    {
        "llm": {
            "provider": "openrouter",
            "api_key": "sk-or-...",
            "model": "meta-llama/llama-3.1-8b-instruct:free"
        }
    }

The :class:`Config` class is the only thing that should read or write this file.
Everything else in the codebase goes through it, so the on-disk format can
change in one place without touching callers.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# The free model we default to. It requires no credit card and is capable
# enough for parsing README files. OpenRouter occasionally retires free models
# or rate-limits them, so the README parser also tries the FALLBACK_MODELS list
# (see ai/readme_parser.py) before giving up. Users can override the default via:
#     devready config set llm openrouter --model <model>
DEFAULT_MODEL = "openai/gpt-oss-20b:free"
DEFAULT_PROVIDER = "openrouter"

# OpenRouter API keys all start with this prefix (e.g. "sk-or-v1-..."). A very
# common mistake is pasting an OpenAI key ("sk-proj-..." / "sk-...") instead,
# which OpenRouter rejects with a 401. We use this to warn early, before a key
# silently fails mid-setup.
OPENROUTER_KEY_PREFIX = "sk-or-"


def openrouter_key_warning(api_key: Optional[str]) -> Optional[str]:
    """Return a friendly warning if ``api_key`` doesn't look like an OpenRouter key.

    Returns ``None`` when the key looks valid (or is empty — nothing to warn
    about). The message is suitable for showing in both the CLI and the GUI.
    """
    key = (api_key or "").strip()
    if not key or key.startswith(OPENROUTER_KEY_PREFIX):
        return None
    if key.startswith("sk-"):
        return (
            "That looks like an OpenAI key — OpenRouter keys start with 'sk-or-'. "
            "Get a free one at https://openrouter.ai/keys"
        )
    return (
        "That doesn't look like an OpenRouter key (they start with 'sk-or-'). "
        "Get a free one at https://openrouter.ai/keys"
    )


def config_dir() -> Path:
    """Return the directory holding DevReady's config (``~/.devready``).

    We resolve the home directory at call time (not import time) so tests can
    redirect it by patching ``Path.home`` or the ``HOME`` env var.
    """
    return Path.home() / ".devready"


def config_path() -> Path:
    """Return the full path to ``config.json``."""
    return config_dir() / "config.json"


# -----------------------------------------------------------------------------
# Project registry — the list of projects DevReady has set up (for `devready list`)
# -----------------------------------------------------------------------------
def projects_path() -> Path:
    """Return the path to the project registry (``~/.devready/projects.json``)."""
    return config_dir() / "projects.json"


def _load_projects() -> List[Dict[str, str]]:
    path = projects_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("projects", [])
        except (json.JSONDecodeError, OSError):
            return []
    return []


def register_project(project_dir: Path) -> None:
    """Record (or refresh) a project in the global registry.

    Called by ``devready start`` so ``devready list`` can later show every
    project the user has set up, newest activity first.
    """
    resolved = str(Path(project_dir).resolve())
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    projects = [p for p in _load_projects() if p.get("path") != resolved]
    projects.insert(0, {"path": resolved, "last_setup": now})

    config_dir().mkdir(parents=True, exist_ok=True)
    projects_path().write_text(
        json.dumps({"projects": projects}, indent=2), encoding="utf-8"
    )


def unregister_project(project_dir: Path) -> bool:
    """Remove a project from the registry. Returns True if it was present.

    Used by ``My Projects`` in the GUI to drop a project from the list (e.g. one
    whose folder was deleted, or that the user no longer wants tracked).
    """
    resolved = str(Path(project_dir).resolve())
    projects = _load_projects()
    remaining = [p for p in projects if p.get("path") != resolved]
    if len(remaining) == len(projects):
        return False
    config_dir().mkdir(parents=True, exist_ok=True)
    projects_path().write_text(
        json.dumps({"projects": remaining}, indent=2), encoding="utf-8"
    )
    return True


def list_projects() -> List[Dict[str, str]]:
    """Return the registered projects (most recently set up first)."""
    return _load_projects()


@dataclass
class LLMSettings:
    """LLM-related settings, mirroring the ``"llm"`` object in config.json."""

    provider: str = DEFAULT_PROVIDER
    api_key: Optional[str] = None
    model: str = DEFAULT_MODEL

    @property
    def is_configured(self) -> bool:
        """True when we have enough to call the LLM (i.e. an API key)."""
        return bool(self.api_key)


@dataclass
class Config:
    """In-memory view of DevReady's configuration plus load/save helpers."""

    llm: LLMSettings = field(default_factory=LLMSettings)
    # Optional GitHub token — raises the Discover search rate limit. Never
    # required; browsing works without it, just with a lower limit.
    github_token: Optional[str] = None

    # -- Loading -------------------------------------------------------------
    @classmethod
    def load(cls) -> "Config":
        """Load config from disk, returning sensible defaults if absent.

        An API key set via the ``OPENROUTER_API_KEY`` environment variable
        takes precedence — handy for CI or for users who prefer not to write
        secrets to disk.
        """
        data: Dict[str, Any] = {}
        path = config_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # A corrupt config shouldn't crash the tool — fall back to
                # defaults and let the user reconfigure.
                data = {}

        llm_data = data.get("llm", {})
        llm = LLMSettings(
            provider=llm_data.get("provider", DEFAULT_PROVIDER),
            api_key=llm_data.get("api_key"),
            model=llm_data.get("model", DEFAULT_MODEL),
        )

        # Environment variable wins over the stored key.
        env_key = os.environ.get("OPENROUTER_API_KEY")
        if env_key:
            llm.api_key = env_key

        github_token = data.get("github", {}).get("token") or os.environ.get("GITHUB_TOKEN")

        return cls(llm=llm, github_token=github_token)

    # -- Saving --------------------------------------------------------------
    def save(self) -> None:
        """Write the current config to disk, creating the directory if needed.

        The file is written with mode 0o600 (owner read/write only) because it
        can contain an API key. On Windows the chmod is a no-op but harmless.
        """
        directory = config_dir()
        directory.mkdir(parents=True, exist_ok=True)

        payload = {
            "llm": {
                "provider": self.llm.provider,
                "api_key": self.llm.api_key,
                "model": self.llm.model,
            },
            "github": {"token": self.github_token},
        }
        path = config_path()
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # Best effort; not all filesystems support chmod.

    # -- Convenience mutators ------------------------------------------------
    def set_llm(
        self,
        provider: str,
        *,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        """Update LLM settings and persist them in one call."""
        self.llm.provider = provider
        if api_key is not None:
            self.llm.api_key = api_key
        if model is not None:
            self.llm.model = model
        self.save()

    def set_github_token(self, token: Optional[str]) -> None:
        """Store (or clear) the optional GitHub token and persist it."""
        self.github_token = token or None
        self.save()
