"""Tests for the Python interpreter resolution helpers.

These cover the pure logic (version parsing, matching) and the "reuse the
running interpreter" fast path. The uv-download path is intentionally not
exercised here — it touches the network and the filesystem, so it's verified
manually rather than in unit tests.
"""

import sys

from devready.environment.version_manager import (
    _interpreter_version,
    _parse_version,
    find_installed_python,
    resolve_python_interpreter,
)


def test_parse_version_major_minor():
    assert _parse_version("3.11") == (3, 11)


def test_parse_version_with_patch():
    # Patch level is ignored — we match on major.minor.
    assert _parse_version("3.11.4") == (3, 11)


def test_parse_version_invalid():
    assert _parse_version("not-a-version") is None
    assert _parse_version("3") is None  # need at least major.minor


def test_interpreter_version_of_current_python():
    # The interpreter running the tests should report its own version.
    expected = (sys.version_info.major, sys.version_info.minor)
    assert _interpreter_version(sys.executable) == expected


def test_resolve_none_returns_current_interpreter():
    # With no required version, any Python works -> the current one.
    assert resolve_python_interpreter(None) == sys.executable


def test_find_matches_running_interpreter():
    # Asking for the exact version we're running on must return this executable,
    # without consulting any external tool.
    current = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert find_installed_python(current) == sys.executable
