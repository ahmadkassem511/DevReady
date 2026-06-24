"""Command-line interface for DevReady (built with Typer).

This module defines every ``devready <command>`` the user can run. It is kept
thin on purpose: each command parses its arguments, then hands off to
:class:`devready.engine.Engine` or the config layer. Put behaviour in those
modules, not here — that keeps the commands testable and the CLI readable.

Commands:
    devready start                      Run the full setup pipeline, then launch.
    devready run                        Relaunch an already-set-up project (fast).
    devready list                       List all projects DevReady has set up.
    devready status                     Show whether the project is running.
    devready stop                       Stop the running server/services.
    devready clean                      Remove DevReady-managed artifacts.
    devready doctor                     Diagnose the local toolchain/config.
    devready config set llm ...         Configure the LLM provider/model/key.
    devready config show                Print the current configuration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from . import __version__
from .config import Config, openrouter_key_warning
from .engine import Engine
from .utils import console, print_banner

# The top-level Typer app. `no_args_is_help` shows usage when run bare.
app = typer.Typer(
    help="DevReady — set up any cloned project with a single command.",
    no_args_is_help=True,
    add_completion=False,
)

# A sub-app groups the `config` commands (e.g. `devready config set ...`).
config_app = typer.Typer(help="View and change DevReady configuration.")
app.add_typer(config_app, name="config")


# -----------------------------------------------------------------------------
# Global --version flag
# -----------------------------------------------------------------------------
def _version_callback(value: bool) -> None:
    """Print the version and exit when ``--version`` is passed."""
    if value:
        console.print(f"DevReady {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show the version and exit.",
        callback=_version_callback,
        is_eager=True,  # evaluate before any command runs
    ),
) -> None:
    """Root callback — exists so ``--version`` works without a subcommand."""


# -----------------------------------------------------------------------------
# Primary commands
# -----------------------------------------------------------------------------
@app.command()
def start(
    path: Path = typer.Argument(
        Path("."),
        help="Project directory to set up (defaults to the current directory).",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Non-interactive: accept every prompt's default (unattended setup).",
    ),
) -> None:
    """Detect, set up, and launch the project in PATH."""
    config = Config.load()

    # First-run UX: if the LLM isn't configured, show the (optional) guide for
    # getting a free key, then continue with the regex fallback regardless.
    if not config.llm.is_configured:
        _show_openrouter_guide()

    ok = Engine(project_dir=path, config=config, assume_yes=yes).start()
    # Exit non-zero when setup failed so automation (CI, the GUI's install job)
    # can tell a real failure from a clean success.
    if not ok:
        raise typer.Exit(code=1)


@app.command()
def run(
    path: Path = typer.Argument(
        Path("."),
        help="Project directory to launch (defaults to the current directory).",
    ),
) -> None:
    """Relaunch an already-set-up project — fast, skips setup.

    Use this for everyday running once ``devready start`` has set the project up.
    """
    Engine(project_dir=path).run()


@app.command()
def status(
    path: Path = typer.Argument(Path("."), help="Project directory."),
) -> None:
    """Show whether the project's server/services are running."""
    Engine(project_dir=path).status()


@app.command()
def stop(
    path: Path = typer.Argument(Path("."), help="Project directory."),
) -> None:
    """Stop the running server and any services DevReady started."""
    Engine(project_dir=path).stop()


@app.command()
def clean(
    path: Path = typer.Argument(Path("."), help="Project directory."),
) -> None:
    """Remove DevReady-managed artifacts (.venv and saved state)."""
    Engine(project_dir=path).clean()


@app.command()
def doctor(
    path: Path = typer.Argument(
        Path("."),
        help="Project to analyse (defaults to the current directory).",
    ),
) -> None:
    """Diagnose the local toolchain, and show a project's requirement plan.

    Run inside a project (or pass its PATH) to see what it needs vs. what's
    installed — and what DevReady will set up — *before* running ``start``.
    """
    Engine(project_dir=path).doctor()


@app.command("list")
def list_projects_cmd() -> None:
    """List every project DevReady has set up, with its run status."""
    Engine.list_all()


@app.command()
def ui(
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Don't auto-open the browser; just print the URL."
    ),
) -> None:
    """Launch the browser GUI — the easy, point-and-click way to use DevReady.

    Starts a small web server on your own machine (127.0.0.1 only) and opens it
    in your browser. From there you can browse vetted apps and install one with
    a click — no terminal commands needed. This is the "easy app" experience.
    """
    try:
        import fastapi  # noqa: F401  (probe: the GUI needs these extras)
        import uvicorn  # noqa: F401
    except ImportError:
        console.print(
            "[error]The web GUI needs a couple of extra packages.[/error]\n"
            'Install them with:  [bold]pip install "devready[ui]"[/bold]'
        )
        raise typer.Exit(code=1)

    from .web.server import serve

    serve(open_browser=not no_browser)


# -----------------------------------------------------------------------------
# config sub-commands
# -----------------------------------------------------------------------------
@config_app.command("set")
def config_set(
    target: str = typer.Argument(..., help="What to configure. Currently: 'llm'."),
    provider: str = typer.Argument(..., help="LLM provider, e.g. 'openrouter'."),
    model: Optional[str] = typer.Option(
        None, "--model", help="Model id, e.g. meta-llama/llama-3.1-8b-instruct:free"
    ),
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        help="API key. If omitted, you'll be prompted (input hidden).",
    ),
) -> None:
    """Configure the LLM, e.g. ``devready config set llm openrouter --model ...``."""
    if target != "llm":
        console.print(f"[error]Unknown config target '{target}'. Try 'llm'.[/error]")
        raise typer.Exit(code=1)

    config = Config.load()

    # Prompt for the key (hidden) only if one wasn't supplied and none exists.
    if api_key is None and not config.llm.api_key:
        entered = typer.prompt("OpenRouter API key", hide_input=True, default="", show_default=False)
        api_key = entered or None

    # Catch the common "OpenAI key pasted instead of OpenRouter" mistake. For the
    # CLI (technical users) we warn and let them override, in case they use a
    # proxy/gateway with a non-standard key.
    if provider == "openrouter":
        warning = openrouter_key_warning(api_key)
        if warning:
            console.print(f"[warning]{warning}[/warning]")
            if not typer.confirm("Save this key anyway?", default=False):
                console.print("[muted]Cancelled — key not saved.[/muted]")
                raise typer.Exit(code=1)

    config.set_llm(provider, api_key=api_key, model=model)
    console.print(
        f"[success]Saved.[/success] provider={config.llm.provider} "
        f"model={config.llm.model} key={'set' if config.llm.api_key else 'not set'}"
    )


@config_app.command("show")
def config_show() -> None:
    """Print the current configuration (the API key is masked)."""
    config = Config.load()
    key = config.llm.api_key
    masked = f"{key[:6]}…{key[-4:]}" if key and len(key) > 10 else ("set" if key else "not set")
    console.print(f"provider: {config.llm.provider}")
    console.print(f"model:    {config.llm.model}")
    console.print(f"api_key:  {masked}")


# -----------------------------------------------------------------------------
# First-run helper
# -----------------------------------------------------------------------------
def _show_openrouter_guide() -> None:
    """Show the 3-step guide for getting a free OpenRouter key.

    Displayed on the first ``start`` when no key is configured. We never block
    on this — DevReady works without a key via the regex parser — so we only
    inform and continue.
    """
    print_banner("[bold]Optional:[/bold] enable smarter README parsing (free)")
    console.print(
        "DevReady can use a [bold]free[/bold] LLM to read messy READMEs more "
        "accurately. It's optional — without it we use an offline parser.\n"
    )
    console.print("To enable it (no credit card required):")
    console.print("  1. Visit [link=https://openrouter.ai/keys]https://openrouter.ai/keys[/link]")
    console.print("  2. Sign in and click [bold]Create Key[/bold].")
    console.print("  3. Run: [bold]devready config set llm openrouter[/bold] and paste the key.\n")
    console.print("[muted]Continuing now with the offline parser…[/muted]")


# Allow ``python -m devready`` / direct execution for convenience.
if __name__ == "__main__":
    app()
