"""Generate a working ``.env`` file for the project.

Most projects ship a ``.env.example`` template, and/or mention required
environment variables in the README. This module merges those sources into a
ready-to-use ``.env`` with sensible development defaults, generating secure
random values for anything that looks like a secret.

Safety notes:
  * We never overwrite an existing ``.env`` unless explicitly told to — it may
    contain real credentials the user added.
  * Generated secrets are for *local development only*. We say so in a comment
    at the top of the file.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Dict, Optional

from ..utils import console

# Substrings that indicate a variable holds a secret and should get a random
# value rather than a placeholder.
_SECRET_HINTS = ("secret", "token", "key", "password", "passwd")

# Template filenames projects use, in priority order (first present wins).
_ENV_EXAMPLE_NAMES = (
    ".env.example", ".env.sample", ".env.template", ".env.dist", ".env.local.example",
)

# Reasonable local defaults for well-known variables, so the app can boot.
_KNOWN_DEFAULTS = {
    "PORT": "3000",
    "NODE_ENV": "development",
    "FLASK_ENV": "development",
    "DJANGO_DEBUG": "True",
    "DEBUG": "True",
    "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/app_dev",
    "REDIS_URL": "redis://localhost:6379/0",
}


def _looks_secret(name: str) -> bool:
    """Return True when a variable name suggests it holds a secret."""
    lowered = name.lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


def _default_value_for(name: str) -> str:
    """Pick a sensible default for a variable name.

    Order of preference:
      1. A known default (PORT, DATABASE_URL, …).
      2. A freshly generated random token if the name looks secret.
      3. An empty string the user can fill in.
    """
    if name in _KNOWN_DEFAULTS:
        return _KNOWN_DEFAULTS[name]
    if _looks_secret(name):
        # URL-safe token; plenty of entropy for a dev secret.
        return secrets.token_urlsafe(32)
    return ""


def _parse_example(example_text: str) -> Dict[str, str]:
    """Parse a ``.env.example`` into an ordered {name: example_value} mapping."""
    parsed: Dict[str, str] = {}
    for raw in example_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        parsed[name.strip()] = value.strip()
    return parsed


def generate_env_file(
    project_dir: Path,
    *,
    readme_env_vars: Optional[Dict[str, str]] = None,
    interactive: bool = True,
    overwrite: bool = False,
) -> Optional[Path]:
    """Create a ``.env`` file from .env.example + README hints.

    Args:
        project_dir: The project root.
        readme_env_vars: Variables (name -> description) the README mentioned,
            typically from :func:`devready.ai.parse_readme`.
        interactive: When True, prompt the user to fill any variable that has
            no default value. When False, leave such variables blank.
        overwrite: Allow replacing an existing ``.env``. Off by default to
            protect real credentials.

    Returns:
        The path to the written ``.env``, or None if nothing was written
        (no variables found, or an existing file we declined to overwrite).
    """
    env_path = project_dir / ".env"
    if env_path.exists() and not overwrite:
        console.print("  [muted].env already exists — leaving it untouched.[/muted]")
        return None

    # Merge the two sources. The example file is authoritative for names/order;
    # README-discovered vars fill in anything the example missed.
    variables: Dict[str, str] = {}

    # Projects name their template various things — use the first one present.
    for example_name in _ENV_EXAMPLE_NAMES:
        example = project_dir / example_name
        if example.exists():
            for name, example_value in _parse_example(example.read_text(encoding="utf-8")).items():
                # Keep a non-empty example value as the default; otherwise derive one.
                variables[name] = example_value or _default_value_for(name)
            break

    for name in (readme_env_vars or {}):
        variables.setdefault(name, _default_value_for(name))

    if not variables:
        console.print("  [muted]No environment variables detected — skipping .env.[/muted]")
        return None

    # Prompt for any still-empty, non-secret values when interactive.
    if interactive:
        for name, value in list(variables.items()):
            if value == "" and not _looks_secret(name):
                entered = console.input(f"  Value for [bold]{name}[/bold] (blank to skip): ").strip()
                if entered:
                    variables[name] = entered

    # Write the file with a clear header explaining its origin.
    lines = [
        "# Generated by DevReady — development defaults only.",
        "# Secret values below are randomly generated for local use; replace",
        "# them with real credentials before deploying anywhere.",
        "",
    ]
    lines.extend(f"{name}={value}" for name, value in variables.items())
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    console.print(f"  [success]Wrote {len(variables)} variables to .env[/success]")
    return env_path
