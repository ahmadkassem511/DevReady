"""DevReady — set up any cloned project with a single command.

This package exposes the core building blocks used by the CLI:

    from devready.engine import Engine          # orchestrates the whole flow
    from devready.config import Config          # reads/writes ~/.devready/config.json
    from devready.detectors import detect_stack # identifies languages/frameworks

The public version string is read by ``devready --version``.
"""

# Single source of truth for the version. Keep this in sync with the version
# declared in pyproject.toml when you cut a release.
__version__ = "0.28.0"

__all__ = ["__version__"]
