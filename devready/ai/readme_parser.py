"""Turn a project's README into structured setup instructions.

There are two strategies, chosen automatically:

* **LLM strategy** (preferred): if the user has configured an OpenRouter API
  key, we ask a free model to read the README and return clean JSON describing
  the install commands, system packages, environment variables, and database
  steps. This handles the messy, free-form prose real READMEs are written in.

* **Regex fallback**: if there's no API key (or the API call fails), we fall
  back to a deterministic, dependency-free parser that scrapes fenced code
  blocks and shell-prompt lines. It's less smart but always available and never
  sends data over the network.

Both strategies return the same :class:`ReadmeInsights` object so callers don't
care which one ran.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..config import Config
from ..utils import console

# Endpoint for OpenRouter's OpenAI-compatible chat completions API.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Instruction sent to the model. We are explicit about the exact JSON shape we
# want so the response is easy and safe to parse.
SYSTEM_PROMPT = (
    "You are a build assistant. Extract setup information from the README the "
    "user provides. Return ONLY a JSON object (no markdown, no prose) with "
    "exactly these keys:\n"
    '  "commands": array of shell commands to install/build the project,\n'
    '  "system_packages": array of OS-level packages required (e.g. ffmpeg),\n'
    '  "env_vars": object mapping each required env var name to a short '
    "description,\n"
    '  "db_commands": array of database setup/migration commands.\n'
    "Use empty arrays/objects when something is not mentioned."
)


@dataclass
class ReadmeInsights:
    """Structured setup information extracted from a README.

    Attributes:
        commands: Install/build commands found in the README.
        system_packages: OS-level packages the project needs.
        env_vars: Required environment variables -> human description.
        db_commands: Database setup / migration commands.
        source: Which strategy produced this, "llm" or "regex" (or "none").
    """

    commands: List[str] = field(default_factory=list)
    system_packages: List[str] = field(default_factory=list)
    env_vars: Dict[str, str] = field(default_factory=dict)
    db_commands: List[str] = field(default_factory=list)
    source: str = "none"

    @property
    def is_empty(self) -> bool:
        """True when we found nothing actionable."""
        return not (self.commands or self.system_packages or self.env_vars or self.db_commands)


def parse_readme(readme_text: str, config: Config) -> ReadmeInsights:
    """Parse a README, preferring the LLM and falling back to regex.

    Args:
        readme_text: Raw contents of README.md.
        config: Loaded :class:`Config`; its LLM settings decide the strategy.

    Returns:
        A :class:`ReadmeInsights`. Never raises — any failure degrades to the
        regex parser so ``devready start`` keeps working offline.
    """
    if not readme_text.strip():
        return ReadmeInsights(source="none")

    if config.llm.is_configured:
        insights = _parse_with_llm(readme_text, config)
        if insights is not None:
            return insights
        # LLM failed (network/quota/parse). Tell the user and fall through.
        console.print("[warning]AI parsing unavailable — using the offline parser.[/warning]")

    return _parse_with_regex(readme_text)


# -----------------------------------------------------------------------------
# LLM strategy
# -----------------------------------------------------------------------------
def _parse_with_llm(readme_text: str, config: Config) -> Optional[ReadmeInsights]:
    """Ask OpenRouter to extract setup info. Returns None on any failure.

    We import httpx lazily so that the regex-only path has zero import cost and
    so a missing/old httpx never breaks the offline workflow.
    """
    try:
        import httpx
    except ImportError:
        return None

    # Cap the README size we send. This keeps us inside the free model's
    # context window and avoids wasting tokens on enormous READMEs.
    excerpt = readme_text[:12000]

    payload = {
        "model": config.llm.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": excerpt},
        ],
        # Low temperature -> deterministic, structured output.
        "temperature": 0.1,
    }
    headers = {
        "Authorization": f"Bearer {config.llm.api_key}",
        "Content-Type": "application/json",
        # OpenRouter asks for these to identify the calling app (optional).
        "HTTP-Referer": "https://github.com/ahmadkassem511/DevReady",
        "X-Title": "DevReady",
    }

    try:
        response = httpx.post(OPENROUTER_URL, json=payload, headers=headers, timeout=60)
        response.raise_for_status()
        # OpenRouter mirrors the OpenAI schema: the text lives at
        # choices[0].message.content.
        content = response.json()["choices"][0]["message"]["content"]
    except Exception:
        # Any network/HTTP/shape error -> let the caller fall back to regex.
        return None

    data = _extract_json(content)
    if data is None:
        return None

    return ReadmeInsights(
        commands=_as_str_list(data.get("commands")),
        system_packages=_as_str_list(data.get("system_packages")),
        env_vars=_as_str_dict(data.get("env_vars")),
        db_commands=_as_str_list(data.get("db_commands")),
        source="llm",
    )


def _extract_json(text: str) -> Optional[dict]:
    """Pull a JSON object out of the model's reply.

    Even when asked for raw JSON, models sometimes wrap it in ```json fences or
    add a sentence. We first try to parse the whole string, then fall back to
    grabbing the outermost ``{...}`` block.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


# -----------------------------------------------------------------------------
# Regex fallback strategy
# -----------------------------------------------------------------------------
# Commands we treat as "install-ish" when scanning prose lines. Conservative on
# purpose — we'd rather miss a command than suggest something destructive.
_INSTALL_KEYWORDS = (
    "pip install",
    "pip3 install",
    "npm install",
    "npm ci",
    "yarn",
    "pnpm install",
    "poetry install",
    "pipenv install",
    "make ",
    "docker compose",
    "docker-compose",
)


def _parse_with_regex(readme_text: str) -> ReadmeInsights:
    """Heuristically extract setup commands without any network access.

    Strategy:
      * Pull commands out of fenced code blocks (```...```), stripping shell
        prompt markers like ``$`` and ``>``.
      * Also scan plain lines that start with a known install keyword.
      * Detect environment variables mentioned as ``UPPER_SNAKE_CASE=`` or in
        ``export FOO=...`` form.
    """
    commands: List[str] = []
    env_vars: Dict[str, str] = {}

    # 1. Fenced code blocks are where READMEs put runnable commands.
    for block in re.findall(r"```(?:[a-zA-Z]*)\n(.*?)```", readme_text, re.DOTALL):
        for line in block.splitlines():
            cleaned = _clean_command_line(line)
            if cleaned and _looks_like_command(cleaned):
                commands.append(cleaned)
            _collect_env_var(cleaned or line, env_vars)

    # 2. Inline lines beginning with a shell prompt, e.g. "$ pip install foo".
    for line in readme_text.splitlines():
        if line.lstrip().startswith(("$", ">")):
            cleaned = _clean_command_line(line)
            if cleaned and _looks_like_command(cleaned):
                commands.append(cleaned)

    # De-duplicate commands while preserving the order they appeared in.
    deduped: List[str] = []
    for cmd in commands:
        if cmd not in deduped:
            deduped.append(cmd)

    return ReadmeInsights(commands=deduped, env_vars=env_vars, source="regex")


def _clean_command_line(line: str) -> str:
    """Strip shell prompt markers and surrounding whitespace from a line."""
    stripped = line.strip()
    # Remove a leading "$ " or "> " prompt, if present.
    stripped = re.sub(r"^[\$>]\s*", "", stripped)
    return stripped


def _looks_like_command(line: str) -> bool:
    """Decide whether a cleaned line is an install/build command worth keeping."""
    lowered = line.lower()
    return any(keyword in lowered for keyword in _INSTALL_KEYWORDS)


def _collect_env_var(line: str, env_vars: Dict[str, str]) -> None:
    """Record an environment variable assignment if the line contains one."""
    match = re.search(r"\b([A-Z][A-Z0-9_]{2,})\s*=", line)
    if match:
        name = match.group(1)
        # Don't overwrite a description we may have set elsewhere.
        env_vars.setdefault(name, "Detected in README")


# -----------------------------------------------------------------------------
# Small coercion helpers — keep the LLM's (untrusted) output well-typed.
# -----------------------------------------------------------------------------
def _as_str_list(value: object) -> List[str]:
    """Coerce a value into a clean list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _as_str_dict(value: object) -> Dict[str, str]:
    """Coerce a value into a {str: str} mapping."""
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}
