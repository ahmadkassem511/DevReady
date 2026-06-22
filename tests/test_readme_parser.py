"""Tests for the README parser's offline regex strategy.

We focus on the regex path because it runs without any network access. The LLM
path is exercised indirectly: when no API key is configured, ``parse_readme``
must fall back to regex, which is what these tests assert.
"""

from devready.ai.readme_parser import ReadmeInsights, parse_readme
from devready.config import Config, LLMSettings

README = """\
# Cool Project

## Setup

```bash
$ pip install -r requirements.txt
npm install
```

Set the following environment variables:

```
DATABASE_URL=postgres://localhost/app
SECRET_KEY=changeme
```
"""


def _config_without_llm() -> Config:
    """A Config whose LLM is intentionally unconfigured (no api_key)."""
    return Config(llm=LLMSettings(api_key=None))


def test_regex_extracts_commands():
    insights = parse_readme(README, _config_without_llm())

    assert insights.source == "regex"
    assert "pip install -r requirements.txt" in insights.commands
    assert "npm install" in insights.commands


def test_regex_extracts_env_vars():
    insights = parse_readme(README, _config_without_llm())

    assert "DATABASE_URL" in insights.env_vars
    assert "SECRET_KEY" in insights.env_vars


def test_empty_readme_yields_empty_insights():
    insights = parse_readme("", _config_without_llm())
    assert isinstance(insights, ReadmeInsights)
    assert insights.is_empty


def test_commands_are_deduplicated():
    text = "```bash\nnpm install\nnpm install\n```"
    insights = parse_readme(text, _config_without_llm())
    assert insights.commands.count("npm install") == 1
