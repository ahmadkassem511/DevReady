"""DevReady's optional local web GUI — the "easy app" for non-technical users.

This sub-package is what powers ``devready ui``: a small web server that runs
**only on the user's own machine** (bound to 127.0.0.1) and serves a browser
interface for browsing a curated catalog of projects, installing one with a
click, and watching the setup progress live.

Nothing here talks to a DevReady cloud — there is no DevReady cloud. The browser
talks to a local server, which drives the same :class:`devready.engine.Engine`
the CLI uses. An OpenRouter API key, if the user enters one, is stored locally
with owner-only permissions and only ever sent directly to OpenRouter.

Security model (see ``security.py``):
  * The server binds to 127.0.0.1 only — never reachable from the network.
  * Every ``/api`` call requires a random per-launch token (defeats other
    local pages / processes silently driving it — DNS-rebinding / CSRF).
  * Host/Origin headers are validated on every request.

The web layer is an optional install: ``pip install "devready[ui]"``.
"""

__all__: list = []
