<div align="center">

<img src="assets/logo.png" alt="DevReady logo" width="140" />

# DevReady

### Set up any cloned project with a single command.

`git clone` → `cd project` → **`devready start`** → done.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.9-blue.svg)](https://www.python.org/)
[![GitHub stars](https://img.shields.io/github/stars/ahmadkassem511/DevReady?style=social)](https://github.com/ahmadkassem511/DevReady)
[![GitHub issues](https://img.shields.io/github/issues/ahmadkassem511/DevReady)](https://github.com/ahmadkassem511/DevReady/issues)

</div>

---

## The problem

You clone a promising repo, and then the README marathon begins: install the
right Python or Node version, create a virtualenv, copy `.env.example`, install
system packages you've never heard of, start a database, run migrations, and
*finally* find the start command. Thirty minutes later you might have it
running — or you've given up.

**DevReady reads the project for you and does all of that automatically.**

## What it does

When you run `devready start` in a freshly cloned project, it walks through
eight steps:

| Step | What happens |
|------|--------------|
| 1. **Detect** | Scans for `package.json`, `requirements.txt`, `pyproject.toml`, etc. to identify languages, frameworks, and required versions. |
| 2. **Read the README** | Uses a **free** LLM (via OpenRouter) — or an offline parser — to extract install commands, system packages, env vars, and DB steps from the README. |
| 3. **System packages** | Offers to install OS-level dependencies (ffmpeg, postgres…) via `brew`/`apt`/`choco`, with your permission. Cleans up README-style names (`Node.js 18+` → `nodejs`) and skips language runtimes — those are handled per-project in step 4. |
| 4. **Runtime & deps** | Picks the **correct Python version for this project** (reusing an installed one, or auto-downloading it with [uv](https://github.com/astral-sh/uv) — isolated, no system changes), builds that project's own `.venv`, and installs dependencies. For Node it uses [fnm](https://github.com/Schniz/fnm)/`nvm` for the required version when available, then runs `npm install`. |
| 5. **Environment** | Generates a `.env` from `.env.example` + README hints, with safe random secrets for local dev. |
| 6. **Services** | If a `docker-compose.yml` exists (and Docker is running), offers to start the services. |
| 7. **Migrations** | Detects and runs migrations (Django, Alembic, Knex…). |
| 8. **Launch** | Picks the right start command for the framework (Streamlit, Django, FastAPI, Flask, or your npm `dev`/`start` script), **waits until the server actually responds**, then prints and opens the URL — e.g. `http://localhost:8501`. For CLI/library/pipeline projects with no web server, it instead shows you how to run it (Makefile targets and README commands). |

Every step is **non-destructive and asks before changing your system** where it
matters. DevReady is **100% free for end users** — the optional AI uses a free
model and requires no credit card.

## Installation

```bash
pip install devready
```

> Requires Python ≥ 3.9. Installing DevReady does **not** install your
> projects' dependencies — it installs the tool that sets them up.

To install from source for development:

```bash
git clone https://github.com/ahmadkassem511/DevReady
cd devready
pip install -e ".[dev]"
```

## Quick start

```bash
git clone https://github.com/some/project
cd project
devready start
```

That's it. DevReady prints a detection summary, walks the eight steps, and
launches the app.

## Enabling the free AI parser (optional, recommended)

Real READMEs are messy prose. A small language model reads them far more
reliably than regex. DevReady uses [OpenRouter](https://openrouter.ai)'s **free**
tier so this costs you nothing.

**Get a free key in 3 steps (no credit card):**

1. Go to **<https://openrouter.ai/keys>**
2. Sign in and click **Create Key**.
3. Save it into DevReady:

   ```bash
   devready config set llm openrouter
   # You'll be prompted to paste your key (hidden input).
   ```

Want a different free model? Override it:

```bash
devready config set llm openrouter --model openai/gpt-oss-20b:free
```

> **You don't need to pick a model.** DevReady defaults to a working free model
> and, if it's ever retired or rate-limited, automatically falls back to other
> free models (ending with OpenRouter's `openrouter/free` auto-router). The
> `--model` flag is only there if you have a preference. Browse all free models
> at [openrouter.ai/models?max_price=0](https://openrouter.ai/models?max_price=0)
> — any id ending in `:free` works.

You can also set the key via an environment variable (handy for CI), which
takes precedence over the stored key:

```bash
export OPENROUTER_API_KEY="sk-or-..."
```

> **No key? No problem.** Without a key, DevReady automatically falls back to an
> offline regex parser. Everything still works — it's just a little less smart
> at reading unusual READMEs. Nothing is ever sent over the network in this mode.

## Commands

| Command | Description |
|---------|-------------|
| `devready start [path]` | Run the full detect → set up → launch pipeline. **Use this the first time** (and after `git pull`). |
| `devready run [path]` | **Relaunch a set-up project — fast.** Skips all setup and reuses the saved start command. Use this every day after the first `start`. |
| `devready status [path]` | Show run state, the saved start command, and the URL. |
| `devready stop [path]` | Stop the launched server and any started services. |
| `devready clean [path]` | Remove DevReady-managed artifacts (`.venv`, state). |
| `devready doctor` | Diagnose your toolchain and configuration. |
| `devready config show` | Print the current configuration (key masked). |
| `devready config set llm openrouter [--model M] [--api-key K]` | Configure the LLM. |
| `devready --version` | Print the version. |

`path` defaults to the current directory in every command.

### Typical workflow

```bash
git clone https://github.com/some/project && cd project
devready start     # first time: detect, install, configure, launch → opens the URL
# ...later, to run it again:
devready run       # instant relaunch, no re-setup
```

Because every project has its own `.venv`, Python version, and saved state, you
can `devready start` any number of projects and they never interfere — switch
between them with a plain `cd` and `devready run`.

> **`devready` vs `python -m devready`** — both work. After `pip install`, the
> `devready` command should be available directly. If your shell says
> "command not found", the Python *Scripts* directory isn't on your `PATH`; add
> it (e.g. `C:\Users\<you>\AppData\Roaming\Python\Python3xx\Scripts` on Windows),
> open a new terminal, or just keep using `python -m devready` — it's identical.

## How configuration is stored

DevReady keeps a small config file at `~/.devready/config.json`:

```json
{
  "llm": {
    "provider": "openrouter",
    "api_key": "sk-or-...",
    "model": "openai/gpt-oss-20b:free"
  }
}
```

The file is written with owner-only permissions (`0600`) because it can hold an
API key. Per-project runtime state (the launched server's PID, etc.) lives in
`<project>/.devready/state.json` and is ignored by git.

## Supported stacks (today)

- **Python** — `requirements.txt`, `pyproject.toml`, `setup.py`; version via
  `.python-version`/`requires-python`; frameworks: Django, Flask, FastAPI,
  Celery, Streamlit.
- **Node.js** — `package.json`; version via `.nvmrc`/`engines.node`;
  frameworks: Next.js, React, Vue, Angular, Express, NestJS, Svelte.

Adding a new stack is intentionally easy — see
[Contributing](#contributing--architecture).

## Per-project isolation (no version conflicts)

DevReady never changes your system Python or affects other projects. Each
project gets **two layers of isolation**:

- **Its own `.venv`** — so package versions in project A can't clash with
  project B.
- **The Python version it actually needs** — if a project requires Python 3.11
  but you're on 3.14, DevReady reuses an installed 3.11 if you have one, and
  otherwise downloads it with [uv](https://github.com/astral-sh/uv) into uv's
  private cache (no admin rights, no `PATH` changes, no impact on anything
  else). It then builds that project's `.venv` from the right interpreter — and
  recreates a mismatched `.venv` automatically.

So you can have a 3.9 project, a 3.11 project, and a 3.12 project side by side,
and `devready start` does the right thing for each without you managing
versions by hand.

## Contributing & architecture

DevReady is built to be edited. The codebase is small and each module has a
single, clear job:

```
devready/
├── cli.py                 # Thin Typer CLI — parses args, delegates to Engine.
├── engine.py              # Orchestrates the 8-step pipeline + status/stop/clean/doctor.
├── config.py              # Read/write ~/.devready/config.json (the only place that does).
├── utils.py               # Shared console, safe subprocess runner, OS/package-manager detection.
├── detectors/             # "What is this project?"
│   ├── base.py            #   Detector base class + DetectionResult.
│   ├── python.py          #   Python detector.
│   ├── node.py            #   Node detector.
│   └── __init__.py        #   Registry + detect_stack() entry point.
├── environment/           # "How do we set it up?"
│   ├── system_deps.py     #   Install OS packages (brew/apt/choco) with consent.
│   ├── version_manager.py #   Resolve per-project Python version (reuse or uv), create venvs, run installs.
│   └── env_vars.py        #   Generate a .env with safe dev defaults.
└── ai/
    └── readme_parser.py   # LLM (OpenRouter) + offline regex fallback. Same output shape.
tests/                     # pytest suite (no network needed).
```

**Design principles, so a teammate can pick this up cold:**

- **The CLI is dumb on purpose.** All behaviour lives in `engine.py` and the
  feature modules, so it's easy to unit-test without spawning a process.
- **One door to each side effect.** All shelling-out goes through
  `utils.run_command`; all config I/O goes through `config.Config`. Change the
  implementation once, everywhere benefits.
- **Detectors are pluggable.** To add a stack: create `detectors/<lang>.py`,
  subclass `Detector`, implement `detect()`, and register it in
  `detectors/__init__.py::ALL_DETECTORS`.
- **Two parser strategies, one result.** `ai/readme_parser.py` returns the same
  `ReadmeInsights` whether it used the LLM or regex, so callers never branch.
- **Never surprise the user.** System-changing steps prompt for confirmation,
  and `.env`/source files are never overwritten without an explicit flag.

### Running the tests

```bash
pip install -e ".[dev]"
pytest
```

The suite uses temp directories and stubs out the network, so it runs fast and
offline.

## Roadmap

- More stacks: Go, Rust, Ruby, PHP.
- Auto-install a Node version manager (fnm) the way we do uv for Python.
- `devready start --yes` for fully non-interactive runs.

## License

Licensed under the **Apache License 2.0** — see [LICENSE](LICENSE) and
[NOTICE](NOTICE). You're free to use, modify, and distribute this software,
including commercially, provided you preserve the license and notices.
