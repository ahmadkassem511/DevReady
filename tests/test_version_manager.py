"""Tests for the Python interpreter resolution helpers.

These cover the pure logic (version parsing, matching) and the "reuse the
running interpreter" fast path. The uv-download path is intentionally not
exercised here — it touches the network and the filesystem, so it's verified
manually rather than in unit tests.
"""

import sys

from devready.environment.version_manager import (
    _interpreter_version,
    _node_package_manager,
    _parse_version,
    find_installed_python,
    resolve_python_interpreter,
)


def test_node_package_manager_detection(tmp_path):
    # No lockfile -> npm.
    assert _node_package_manager(tmp_path) == "npm"
    # yarn.lock -> yarn.
    (tmp_path / "yarn.lock").write_text("")
    assert _node_package_manager(tmp_path) == "yarn"
    # pnpm-lock.yaml wins (checked first).
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert _node_package_manager(tmp_path) == "pnpm"


def test_toolchain_auto_installs_missing_runner(tmp_path, monkeypatch):
    # When a language toolchain (e.g. cargo) is missing, setup should install it
    # and continue — not warn and stop.
    import devready.environment.system_deps as sd
    import devready.environment.version_manager as vm

    monkeypatch.setattr(vm, "command_exists", lambda n: False)  # cargo missing
    installed = []
    monkeypatch.setattr(sd, "install_tool", lambda name: installed.append(name) or True)
    ran = []
    monkeypatch.setattr(vm, "run_command", lambda *a, **k: ran.append(a) or vm.CommandResult(command="x", returncode=0))

    out = vm.setup_rust(tmp_path, None)
    assert installed == ["cargo"]   # tried to install the missing toolchain
    assert ran                      # then proceeded to build
    assert out and out[0].ok


def test_toolchain_gives_up_gracefully_when_install_fails(tmp_path, monkeypatch):
    import devready.environment.system_deps as sd
    import devready.environment.version_manager as vm

    monkeypatch.setattr(vm, "command_exists", lambda n: False)
    monkeypatch.setattr(sd, "install_tool", lambda name: False)  # couldn't install
    out = vm.setup_go(tmp_path, None)
    assert out == []  # no build attempted, but no crash


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
