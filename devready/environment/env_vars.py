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

import re
import secrets
from pathlib import Path
from typing import Dict, List, Optional

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

# Known AI provider API key names. When these get random placeholder values,
# the app won't work — we warn the user after generating .env.
_AI_API_KEY_NAMES = {
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "AZURE_OPENAI_API_KEY", "REPLICATE_API_KEY", "HUGGINGFACE_API_KEY",
    "COHERE_API_KEY", "AI21_API_KEY", "MISTRAL_API_KEY", "TOGETHER_API_KEY",
    "GROQ_API_KEY", "PERPLEXITY_API_KEY", "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY", "OPENAI_ORGANIZATION", "OPENAI_BASE_URL",
}


def has_placeholder_api_keys(env_path: Path) -> List[str]:
    """Return names of AI API keys in .env that have random-looking placeholder values.

    Randomly generated tokens from _default_value_for are 43+ char base64url strings.
    Real API keys don't match this pattern — we flag them so the user knows to
    replace them before the app will work.
    """
    if not env_path.exists():
        return []
    try:
        # utf-8-sig: strip a Windows BOM, or "﻿NAME" wouldn't match NAME.
        text = env_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    placeholder = re.compile(r"^[A-Za-z0-9_-]{43,}$")
    found: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name in _AI_API_KEY_NAMES and placeholder.match(value.strip()):
            found.append(name)
    return found


# Where to get each provider's API key — shown next to the input in the GUI's
# "Finish setup" form so a non-technical user knows exactly where to click.
KEY_PROVIDER_URLS = {
    "OPENAI_API_KEY": "https://platform.openai.com/api-keys",
    "ANTHROPIC_API_KEY": "https://console.anthropic.com/settings/keys",
    "GEMINI_API_KEY": "https://aistudio.google.com/apikey",
    "GOOGLE_API_KEY": "https://aistudio.google.com/apikey",
    "DEEPSEEK_API_KEY": "https://platform.deepseek.com/api_keys",
    "OPENROUTER_API_KEY": "https://openrouter.ai/keys",
    "GROQ_API_KEY": "https://console.groq.com/keys",
    "MISTRAL_API_KEY": "https://console.mistral.ai/api-keys",
    "TOGETHER_API_KEY": "https://api.together.ai/settings/api-keys",
    "COHERE_API_KEY": "https://dashboard.cohere.com/api-keys",
    "REPLICATE_API_KEY": "https://replicate.com/account/api-tokens",
    "HUGGINGFACE_API_KEY": "https://huggingface.co/settings/tokens",
    "PERPLEXITY_API_KEY": "https://www.perplexity.ai/settings/api",
}


def key_help_url(name: str) -> str:
    """The provider page where the user creates this key, or an empty string."""
    return KEY_PROVIDER_URLS.get(name, "")


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def set_env_values(project_dir: Path, values: Dict[str, str]) -> List[str]:
    """Write real values for variables into the project's ``.env`` — the write
    half of the first-run secrets wizard.

    Replaces the line for each existing variable in place (preserving order and
    comments) and appends any that aren't in the file yet. Names are validated;
    values are single-line only. Returns the names actually written. Values are
    never logged.
    """
    env_path = project_dir / ".env"
    clean: Dict[str, str] = {}
    for name, value in (values or {}).items():
        name = (name or "").strip()
        value = (value or "").strip()
        if not name or not value or not _ENV_NAME_RE.match(name):
            continue
        if "\n" in value or "\r" in value:
            continue  # a key is always one line — reject anything else
        clean[name] = value
    if not clean:
        return []

    # utf-8-sig: a BOM (e.g. from Notepad/PowerShell) would make the first
    # variable's name compare as "﻿NAME" and dodge in-place replacement —
    # seen live, producing a duplicated key line. Read stripped, write clean.
    lines = (
        env_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        if env_path.exists() else []
    )
    written: List[str] = []
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name = stripped.partition("=")[0].strip()
        if name in clean and name not in written:
            lines[i] = f"{name}={clean[name]}"
            written.append(name)
    for name, value in clean.items():
        if name not in written:
            lines.append(f"{name}={value}")
            written.append(name)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return written


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
        # Hex token — safe for all API key formats and config files.
        return secrets.token_hex(32)
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

    # Check if any known AI API keys ended up as random placeholders — the user
    # needs real keys for the app to work.
    placeholder_keys = has_placeholder_api_keys(env_path)
    if placeholder_keys:
        console.print()
        console.print("  [warning]Some API keys were set to random placeholders — they need real values:[/warning]")
        for key in placeholder_keys:
            console.print(f"    [bold]{key}[/bold] (replace with your real key from the provider)")
        console.print(
            "  [muted]This app won't work without valid API keys.\n"
            "  Edit the .env file and replace the random values with your keys.[/muted]"
        )
        console.print()

    return env_path
