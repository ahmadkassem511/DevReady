"""Security primitives for DevReady's local web GUI.

A local web server that can *install and run code* is a powerful thing, so the
threat we actually defend against is not someone on the internet — the server
isn't reachable from the internet — but **another program on the same machine**
(for example a malicious web page the user is also visiting) trying to silently
issue commands to ``localhost``. This is the classic CSRF / DNS-rebinding attack
against local apps, and it's exactly what Jupyter's token model defends against.

Three layers, all enforced before any handler runs:

1. **Loopback binding** (done in ``server.run_server`` — host = 127.0.0.1) so the
   GUI is never exposed on the network.
2. **A random per-launch token.** Generated when the server starts and embedded
   in the URL we open in the browser. Every ``/api`` request must present it
   (header ``X-DevReady-Token`` or ``?token=``). Without it, no action runs — so
   a random web page cannot drive the server even though it's on localhost.
3. **Host/Origin validation.** We reject any request whose ``Host`` isn't
   localhost, and any cross-origin ``Origin`` — this is what stops DNS-rebinding,
   where an attacker's domain resolves to 127.0.0.1.

The token is compared with :func:`secrets.compare_digest` to avoid timing leaks.
"""

from __future__ import annotations

import secrets

# Hostnames we accept in the Host header. Anything else (e.g. a rebound attacker
# domain pointing at 127.0.0.1) is rejected.
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


def generate_token() -> str:
    """Return a fresh, unguessable session token for one server launch."""
    return secrets.token_urlsafe(32)


def token_matches(expected: str, provided: str | None) -> bool:
    """Constant-time comparison of the expected vs. a provided token."""
    if not provided:
        return False
    return secrets.compare_digest(expected, provided)


def host_is_allowed(host_header: str | None) -> bool:
    """True if the request's Host header points at loopback.

    ``host_header`` looks like ``127.0.0.1:8765`` — we strip the optional port
    before comparing against :data:`ALLOWED_HOSTS`.
    """
    if not host_header:
        return False
    hostname = host_header.rsplit(":", 1)[0].strip("[]")  # drop port; tolerate IPv6 brackets
    return hostname in ALLOWED_HOSTS


def origin_is_allowed(origin_header: str | None) -> bool:
    """True if a request's Origin is same-origin (loopback) or absent.

    A missing Origin is fine — same-origin GETs and direct navigations often omit
    it. A *present* Origin must point at loopback; a foreign site's Origin is
    rejected, which is what blocks a malicious page from scripting our API.
    """
    if not origin_header:
        return True
    # Origin is a full URL like "http://127.0.0.1:8765"; pull out the hostname.
    without_scheme = origin_header.split("://", 1)[-1]
    hostname = without_scheme.rsplit(":", 1)[0].strip("[]")
    return hostname in ALLOWED_HOSTS
