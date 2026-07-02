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


def test_node_satisfies(monkeypatch):
    import devready.environment.version_manager as vm

    monkeypatch.setattr(vm, "_node_version", lambda: "22.21.1")
    assert vm._node_satisfies("22.22") is False   # 22.21 does not meet >=22.22
    assert vm._node_satisfies("22") is True        # any 22.x meets a major-only pin
    assert vm._node_satisfies("18") is True        # 22 is newer than 18
    monkeypatch.setattr(vm, "_node_version", lambda: None)
    assert vm._node_satisfies("18") is False       # no Node installed


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


def test_which_on_path_uses_given_path(tmp_path, monkeypatch):
    import devready.environment.version_manager as vm

    seen = {}
    monkeypatch.setattr(vm.shutil, "which", lambda name, path=None: seen.update({"path": path}) or "/x")
    assert vm._which_on_path("corepack", "CUSTOM") is True
    assert seen["path"] == "CUSTOM"


def test_fnm_node_bin_dir_parses_node_output(tmp_path, monkeypatch):
    import devready.environment.version_manager as vm

    # node prints its exec dir; we return it only if it actually exists.
    monkeypatch.setattr(
        vm, "run_command", lambda *a, **k: vm.CommandResult(command="x", returncode=0, stdout=str(tmp_path))
    )
    assert vm._fnm_node_bin_dir("24.0") == str(tmp_path)


def test_setup_node_pinned_version_puts_bin_on_path_not_fnm_exec(tmp_path, monkeypatch):
    # Regression: a pinned Node version must run the package manager with the
    # pinned Node's bin dir on PATH — NOT via `fnm exec` (which can't spawn the
    # .cmd shims corepack/pnpm are on Windows). This is the gradio failure.
    import devready.environment.version_manager as vm
    from devready.detectors import DetectionResult

    (tmp_path / "pnpm-lock.yaml").write_text("")  # -> pnpm project
    bin_dir = tmp_path / "nodebin"
    bin_dir.mkdir()

    monkeypatch.setattr(vm, "command_exists", lambda n: n in ("npm", "fnm"))
    monkeypatch.setattr(vm, "_node_satisfies", lambda v: False)
    monkeypatch.setattr(vm, "_node_version", lambda: "22.21.1")
    monkeypatch.setattr(vm, "_fnm_node_bin_dir", lambda v: str(bin_dir))
    monkeypatch.setattr(vm, "_which_on_path", lambda name, path=None: name == "corepack")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return vm.CommandResult(command=" ".join(cmd) if isinstance(cmd, list) else cmd, returncode=0)

    monkeypatch.setattr(vm, "run_command", fake_run)

    result = DetectionResult(language="Node.js", version="24.0", frameworks=[], package_files=["package.json"])
    vm.setup_node(tmp_path, result, healer=None)

    # Find the dependency-install call (the one that got a custom env).
    install_calls = [(c, k) for c, k in calls if k.get("env") is not None]
    assert install_calls, "expected the install to run with a custom Node env"
    cmd, kwargs = install_calls[-1]
    assert cmd[0] == "corepack" and "fnm" not in cmd  # NOT routed through `fnm exec`
    assert kwargs["env"]["PATH"].startswith(str(bin_dir))  # pinned Node's bin is first


def test_setup_php_installs_runtime_before_composer(tmp_path, monkeypatch):
    # composer is a PHP app; PHP must be installed too, or `composer install`
    # dies with "php is not recognized". Both runtime and manager must install.
    import devready.environment.system_deps as sd
    import devready.environment.version_manager as vm
    from devready.detectors import DetectionResult

    installed = []
    monkeypatch.setattr(vm, "command_exists", lambda n: False)  # neither php nor composer present
    monkeypatch.setattr(sd, "install_tool", lambda name: installed.append(name) or True)
    monkeypatch.setattr(vm, "run_command", lambda *a, **k: vm.CommandResult(command="x", returncode=0))

    result = DetectionResult(language="PHP", version="8.3", frameworks=[], package_files=["composer.json"])
    vm.setup_php(tmp_path, result)
    assert "php" in installed       # the runtime
    assert "composer" in installed  # and the package manager


def test_needs_bash_script_shell_detects_sh_scripts(tmp_path, monkeypatch):
    import devready.environment.version_manager as vm

    (tmp_path / "package.json").write_text(
        '{"scripts": {"postinstall": "build/demi.sh ci", "dev": "build/demi.sh dev"}}'
    )
    monkeypatch.setattr(vm.sys, "platform", "win32")
    monkeypatch.setattr(vm.shutil, "which", lambda n: r"C:\Git\bash.exe" if n == "bash" else None)
    assert vm.needs_bash_script_shell(tmp_path) == r"C:\Git\bash.exe"


def test_needs_bash_script_shell_none_without_sh(tmp_path, monkeypatch):
    import devready.environment.version_manager as vm

    (tmp_path / "package.json").write_text('{"scripts": {"dev": "vite", "build": "vite build"}}')
    monkeypatch.setattr(vm.sys, "platform", "win32")
    monkeypatch.setattr(vm.shutil, "which", lambda n: r"C:\Git\bash.exe")
    assert vm.needs_bash_script_shell(tmp_path) is None


def test_needs_bash_script_shell_none_on_posix(tmp_path, monkeypatch):
    # On Unix npm already uses sh, which runs .sh fine — no override needed.
    import devready.environment.version_manager as vm

    (tmp_path / "package.json").write_text('{"scripts": {"postinstall": "build/x.sh"}}')
    monkeypatch.setattr(vm.sys, "platform", "linux")
    assert vm.needs_bash_script_shell(tmp_path) is None


def test_git_bash_skips_system32_wsl_stub(tmp_path, monkeypatch):
    # which('bash') returns the System32 WSL launcher first; _git_bash must skip
    # it and resolve a real Git Bash (else npm scripts hit WSL_E_DEFAULT_DISTRO).
    import devready.environment.version_manager as vm

    git_root = tmp_path / "Git"
    (git_root / "bin").mkdir(parents=True)
    bash = git_root / "bin" / "bash.exe"
    bash.write_text("")
    (git_root / "cmd").mkdir(parents=True)
    git_exe = git_root / "cmd" / "git.exe"
    git_exe.write_text("")

    def fake_which(name):
        if name == "bash":
            return r"C:\Windows\System32\bash.exe"  # the WSL stub
        if name == "git":
            return str(git_exe)
        return None

    monkeypatch.setattr(vm.shutil, "which", fake_which)
    assert vm._git_bash() == str(bash)


def test_ensure_php_extensions_enables_openssl(tmp_path, monkeypatch):
    # A fresh PHP with no php.ini must get one created from the template, with the
    # shipped extensions (openssl) enabled — so `composer install` stops failing
    # with "the openssl extension is required".
    import devready.environment.version_manager as vm

    php_dir = tmp_path / "php"
    (php_dir / "ext").mkdir(parents=True)
    (php_dir / "ext" / "php_openssl.dll").write_text("")
    (php_dir / "php.ini-production").write_text(';extension=openssl\n;extension_dir = "ext"\n')
    php_exe = php_dir / "php.exe"
    php_exe.write_text("")

    monkeypatch.setattr(vm, "command_exists", lambda n: n == "php")
    monkeypatch.setattr(
        vm, "run_command",
        lambda *a, **k: vm.CommandResult(command="x", returncode=0, stdout=str(php_exe)),
    )

    vm.ensure_php_extensions()

    ini = (php_dir / "php.ini").read_text()
    assert "extension=openssl" in ini
    assert ";extension=openssl" not in ini       # it was uncommented
    assert 'extension_dir = "ext"' in ini


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


def test_resolve_none_uses_current_when_well_supported(monkeypatch):
    # No required version + a well-supported running Python -> use the current one.
    import devready.environment.version_manager as vm

    monkeypatch.setattr(vm, "_current_python_version", lambda: (3, 11))
    assert vm.resolve_python_interpreter(None) == sys.executable


def test_resolve_none_prefers_stable_on_bleeding_edge(monkeypatch):
    # No required version + a bleeding-edge running Python (e.g. 3.14) -> prefer a
    # broadly-compatible line so packages install from wheels, not source builds.
    import devready.environment.version_manager as vm

    monkeypatch.setattr(vm, "_current_python_version", lambda: (3, 14))
    monkeypatch.setattr(
        vm, "find_installed_python", lambda v: r"C:\py312\python.exe" if v == "3.12" else None
    )
    assert vm.resolve_python_interpreter(None) == r"C:\py312\python.exe"


def test_resolve_none_falls_back_to_current_if_no_stable_available(monkeypatch):
    # Bleeding-edge, but no stable line installed and uv can't fetch one -> current.
    import devready.environment.version_manager as vm

    monkeypatch.setattr(vm, "_current_python_version", lambda: (3, 14))
    monkeypatch.setattr(vm, "find_installed_python", lambda v: None)
    monkeypatch.setattr(vm, "install_python_with_uv", lambda v: None)
    assert vm.resolve_python_interpreter(None) == sys.executable


def test_find_matches_running_interpreter():
    # Asking for the exact version we're running on must return this executable,
    # without consulting any external tool.
    current = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert find_installed_python(current) == sys.executable


# -- easiest-install-path resolver (published package over source build) ------
def _openwebui_like_repo(tmp_path):
    """A repo like Open WebUI: Python package + bundled JS frontend, whose README
    documents the official prebuilt install (`pip install open-webui`)."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "open-webui"\nversion = "1.0"\n', encoding="utf-8"
    )
    (tmp_path / "package.json").write_text('{"name": "frontend"}')
    (tmp_path / "README.md").write_text(
        "## Quick start\n\nInstall via pip:\n\n    pip install open-webui\n"
    )
    return tmp_path


def test_project_package_name_parses_pyproject(tmp_path):
    from devready.environment.version_manager import _project_package_name

    _openwebui_like_repo(tmp_path)
    assert _project_package_name(tmp_path) == "open-webui"
    assert _project_package_name(tmp_path / "nope") is None


def test_published_package_preferred_over_source_build(tmp_path, monkeypatch):
    import devready.environment.version_manager as vm
    from devready.utils import CommandResult

    _openwebui_like_repo(tmp_path)
    calls = []

    def fake_pip_install(venv_python, target_args, project_dir, healer):
        calls.append(target_args)
        return CommandResult("pip install", 0)

    monkeypatch.setattr(vm, "_pip_install", fake_pip_install)
    result = vm._published_package_install("python", tmp_path, None)
    assert result is not None and result.ok
    assert calls == [["open-webui"]]  # installed the wheel, not ["."]


def test_published_package_skipped_without_readme_mention(tmp_path, monkeypatch):
    import devready.environment.version_manager as vm

    _openwebui_like_repo(tmp_path)
    (tmp_path / "README.md").write_text("Build from source with pip install .\n")
    monkeypatch.setattr(vm, "_pip_install", lambda *a: (_ for _ in ()).throw(AssertionError))
    assert vm._published_package_install("python", tmp_path, None) is None


def test_published_package_skipped_without_js_frontend(tmp_path, monkeypatch):
    # A plain Python package builds fast from source — no need to special-case.
    import devready.environment.version_manager as vm

    _openwebui_like_repo(tmp_path)
    (tmp_path / "package.json").unlink()
    monkeypatch.setattr(vm, "_pip_install", lambda *a: (_ for _ in ()).throw(AssertionError))
    assert vm._published_package_install("python", tmp_path, None) is None


def test_published_package_falls_back_to_source_on_failure(tmp_path, monkeypatch):
    import devready.environment.version_manager as vm
    from devready.utils import CommandResult

    _openwebui_like_repo(tmp_path)
    monkeypatch.setattr(
        vm, "_pip_install", lambda *a: CommandResult("pip install", 1, stderr="boom")
    )
    assert vm._published_package_install("python", tmp_path, None) is None
    assert not vm.used_published_package(tmp_path)  # no marker on failure


def test_published_package_marker_written_on_success(tmp_path, monkeypatch):
    # The marker tells the rest of the pipeline the wheel IS the app, so the
    # root npm install and source sub-projects (backend/) are skipped.
    import devready.environment.version_manager as vm
    from devready.utils import CommandResult

    _openwebui_like_repo(tmp_path)
    monkeypatch.setattr(vm, "_pip_install", lambda *a: CommandResult("pip install", 0))
    assert not vm.used_published_package(tmp_path)
    result = vm._published_package_install("python", tmp_path, None)
    assert result is not None and result.ok
    assert vm.used_published_package(tmp_path)
