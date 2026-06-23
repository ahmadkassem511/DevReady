"""Environment-setup helpers: system packages, runtime versions, and .env files.

These modules contain the logic that actually changes the user's machine
(installing packages, creating virtualenvs, writing .env). They are kept
separate from detection so the "what" (detectors) and the "how" (environment)
stay decoupled and individually testable.
"""

from . import env_vars, strategies, system_deps, version_manager

__all__ = ["system_deps", "version_manager", "env_vars", "strategies"]
