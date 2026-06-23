"""Tests for system-package name normalization.

The README parser often yields human-written prerequisite names like
"Python 3.10+" or "Node.js 18+". These must be cleaned (and language runtimes
dropped) before we ever hand them to a package manager — otherwise we'd run
nonsense like `choco install Python 3.10+`.
"""

from devready.environment.system_deps import _normalize_package, normalize_packages


def test_strips_version_specifiers():
    assert _normalize_package("FFmpeg") == "ffmpeg"
    assert _normalize_package("redis v7") == "redis"
    assert _normalize_package("PostgreSQL >= 14") == "postgresql"
    assert _normalize_package("ImageMagick (latest)") == "imagemagick"


def test_node_aliases_to_nodejs():
    assert _normalize_package("Node.js 18+") == "nodejs"
    assert _normalize_package("node") == "nodejs"


def test_runtimes_are_dropped_from_install_list():
    to_install, runtimes = normalize_packages(["Python 3.10+", "Node.js 18+", "FFmpeg"])
    # Only the real system package remains; runtimes are reported separately.
    assert to_install == ["ffmpeg"]
    assert "python" in runtimes
    assert "nodejs" in runtimes


def test_deduplicates_installable_packages():
    to_install, _ = normalize_packages(["ffmpeg", "FFmpeg", "ffmpeg (latest)"])
    assert to_install == ["ffmpeg"]


def test_empty_input():
    assert normalize_packages([]) == ([], [])


def test_install_tool_noop_when_already_present():
    # Python is guaranteed to be on PATH while the tests run, so install_tool
    # must short-circuit to True without trying to install anything.
    from devready.environment.system_deps import install_tool

    assert install_tool("python") is True


def test_tool_packages_have_make_mappings():
    # `make` is the canonical auto-install case; ensure it maps for common managers.
    from devready.environment.system_deps import TOOL_PACKAGES

    assert TOOL_PACKAGES["make"]["choco"] == "make"
    assert TOOL_PACKAGES["make"]["brew"] == "make"
    assert TOOL_PACKAGES["make"]["apt"] == "make"
