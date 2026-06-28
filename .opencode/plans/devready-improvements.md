# DevReady Improvements Implementation Plan

## Step 1: Post-Launch HTTP Verification
**File:** `devready/engine.py`

### 1a. Add httpx import (line 22)
**After:** `import webbrowser`
**Add:** `import httpx`

### 1b. Add HTTP verification method (after `_scan_build_error`, before `_resolve_launch`)
```python
@staticmethod
def _check_response_body(url: str) -> Optional[str]:
    """GET the URL and return a warning if the page seems broken/blank, else None."""
    try:
        resp = httpx.get(url, timeout=5.0, headers={"User-Agent": "DevReady/1.0"})
        body = resp.text.strip()
        if resp.status_code >= 400:
            return f"HTTP {resp.status_code} — the server returned an error"
        if len(body) < 100:
            return "The page appears blank or nearly empty — check the server log for errors"
        # Check for common error patterns in the body
        lowered = body.lower()
        if any(p in lowered for p in ("cannot get", "cannot find", "not found", "internal server error",
                                        "application error", "an error occurred", "missing api key",
                                        "invalid api key", "configuration error")):
            return "The page reports an application error — check your .env configuration"
        return None
    except httpx.RequestError as exc:
        return f"Could not verify the page: {exc}"
    except Exception:
        return None
```

### 1c. Modify `_announce_running` (around line 900-918)
**Change the section that prints the success message for port:**
```python
if port:
    url = f"http://localhost:{port}"
    served.append(url)
    # Check HTTP response content — not just that the port is open
    page_warn = self._check_response_body(url)
    if page_warn:
        console.print(f"  [warning]• {name} started on {url}, but: {page_warn}[/warning]")
        build_err = self._scan_build_error(log_path)
        if build_err:
            console.print(f"  [warning]Server log shows: {build_err}[/warning]")
        console.print("  [muted]Check the log or your .env configuration and re-run.[/muted]")
    else:
        console.print(f"  [success]✓ {name} → {url}[/success]")
    # The server bound its port, but its own code may still have a build error
    build_err = self._scan_build_error(log_path)
    if build_err:
        console.print(
            f"  [warning]Heads up: {name} started, but the project's own code reported "
            f"a build error — the page may show it:[/warning]\n  [muted]{build_err}[/muted]"
        )
    if not opened:
        try:
            webbrowser.open(url)
            opened = True
        except webbrowser.Error:
            pass
```

---

## Step 2: Docker Compose Validation & .env Integration
**File:** `devready/engine.py`

### 2a. Modify `_bring_up_services` (around line 543-554)
**Replace the compose block:**
```python
if compose is not None:
    console.print(f"  Starting services from {compose.name} (docker compose up -d)…")
    # Validate compose config first
    validate = run_command(
        ["docker", "compose", "config", "--no-interpolate"],
        cwd=str(self.project_dir), capture=True,
    )
    if not validate.ok:
        console.print(f"  [warning]Docker Compose configuration has issues:[/warning]")
        for line in validate.stderr.strip().splitlines():
            console.print(f"  [muted]{line}[/muted]")
        stderr_lower = validate.stderr.lower()
        if "no service selected" in stderr_lower:
            console.print(
                "  [warning]The compose file defines no services — variables may resolve "
                "to empty strings. Check your .env file for required values.[/warning]"
            )
        console.print("  [muted]Attempting to start anyway…[/muted]")
    else:
        console.print("  [muted]Compose configuration validated successfully.[/muted]")
    # Use the project's .env so variables in the compose file resolve correctly
    env_file = self.project_dir / ".env"
    compose_cmd = ["docker", "compose", "up", "-d"]
    if env_file.exists():
        compose_cmd = ["docker", "compose", "--env-file", ".env", "up", "-d"]
    result = run_command(compose_cmd, cwd=str(self.project_dir), capture=False, env=svc_env)
    if result.ok:
        console.print("  [success]Services started.[/success]")
        self._write_state(docker=True)
    else:
        console.print("  [error]Failed to start services.[/error]")
    return
```

---

## Step 3: Smart API Key / Required Variable Detection
**File:** `devready/environment/env_vars.py`

### 3a. Add AI API key set (after `_KNOWN_DEFAULTS`, line 41)
```python
# Known AI provider API key names. When these are set to random placeholder values,
# the app will show a blank page or error — we warn the user after generating .env.
_AI_API_KEY_NAMES = {
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "AZURE_OPENAI_API_KEY", "REPLICATE_API_KEY", "HUGGINGFACE_API_KEY",
    "COHERE_API_KEY", "AI21_API_KEY", "MISTRAL_API_KEY", "TOGETHER_API_KEY",
    "GROQ_API_KEY", "PERPLEXITY_API_KEY", "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY", "OPENAI_ORGANIZATION", "OPENAI_BASE_URL",
}
```

### 3b. Add detection function (after `_KNOWN_DEFAULTS`)
```python
def has_placeholder_api_keys(env_path: Path) -> list[str]:
    """Return names of AI API keys in .env that have random-looking placeholder values.

    Randomly generated tokens from _default_value_for are 43+ char base64url strings.
    A real API key from any provider doesn't look like this — but we stay conservative
    and only flag keys whose value matches the token_urlsafe(32) pattern.
    """
    import re
    if not env_path.exists():
        return []
    try:
        text = env_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    placeholder = re.compile(r"^[A-Za-z0-9_-]{43,}$")
    found: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name in _AI_API_KEY_NAMES and placeholder.match(value.strip()):
            found.append(name)
    return found
```

### 3c. Modify `generate_env_file` return (before the final return)
**After the write and console print, add:**
```python
    # Check if any known AI API keys ended up as placeholders
    placeholder_keys = has_placeholder_api_keys(env_path)
    if placeholder_keys:
        console.print()
        console.print("  [warning]Some API keys were set to random placeholders:[/warning]")
        for key in placeholder_keys:
            console.print(f"    [bold]{key}[/bold] (replace with your real key)")
        console.print(
            "  [muted]This app needs real API keys from their respective providers to work.\n"
            "  Edit the .env file and replace the random values with your keys.[/muted]"
        )
    if placeholder_keys:
        console.print()
```

---

## Step 4: Healer Improvements
**File:** `devready/ai/healer.py`

### 4a. Add known-failures DB (after the `_FORBIDDEN_TOKENS` tuple, around line 61)
```python
# Known build-failure signatures that can be diagnosed offline — avoids wasting
# an LLM call (and tokens) on common, well-understood issues. Each entry is a
# (match_pattern, diagnosis_message, is_fixable) tuple. The pattern is searched
# case-insensitively in the error output.
_KNOWN_FAILURES: list[tuple[str, str, bool]] = [
    # Rust / Windows — missing MSVC C++ Build Tools
    ("webview2-com-sys", "Rust's webview2-com-sys crate needs the Windows SDK (MSVC build tools). Install from: https://visualstudio.microsoft.com/visual-cpp-build-tools/", False),
    ("link.exe", "A C/C++ linker was not found. On Windows, install MSVC Build Tools from: https://visualstudio.microsoft.com/visual-cpp-build-tools/", False),
    ("link.exe not found", "The MSVC linker (link.exe) is missing. Install Visual Studio C++ Build Tools.", False),
    # Tauri template placeholder — dev scaffold, not a real crate
    ("tauri-plugin-{{name}}", "A Tauri plugin workspace template wasn't rendered — this is a development scaffold, not an installable crate. Skipping this dependency is safe.", True),
    ("name = \"tauri-plugin-{{", "Tauri plugin template placeholder detected — not a real Rust crate. Safe to skip.", True),
    # npm / Node — common issues
    ("enoent", "A file referenced by the project is missing — check that all required assets exist.", False),
    ("eintegrit", "npm integrity check failed — the lockfile is corrupted. Try deleting node_modules and package-lock.json, then re-run.", True),
    # Python / pip
    ("flash_attn", "flash-attn is a CUDA-only package — it can't build on this machine. Skipping it is safe unless you need GPU attention layers.", True),
    ("cuda_home", "CUDA is not installed — GPU-accelerated packages will fall back to CPU.", False),
    # Docker
    ("no service selected", "Docker Compose found no services — variables in the compose file may resolve to empty strings. Check your .env file.", False),
]
```

### 4b. Modify `run_step` to check known failures before LLM (around line 105)
**Before the LLM healing loop, after the built-in retries, add:**
```python
        # 1.5. Check known-failure signatures (offline, no LLM needed).
        known_result = self._check_known_failures(result.stdout, result)
        if known_result is not None:
            return known_result
```

### 4c. Add `_check_known_failures` method
```python
    def _check_known_failures(self, error_text: str, current_result: CommandResult) -> Optional[CommandResult]:
        """Check error output against known-failure signatures before calling the LLM.

        Returns a CommandResult if the failure was handled (e.g., skipped), or None
        to proceed to the LLM healing loop.
        """
        lowered = (error_text or "").lower()
        for pattern, diagnosis, fixable in _KNOWN_FAILURES:
            if pattern in lowered:
                console.print(f"  [info]Diagnosis:[/info] {diagnosis}")
                if fixable:
                    # For fixable known failures, we tell the user and treat as recoverable
                    console.print("  [muted]Continuing — this is not a blocking error.[/muted]")
                    return CommandResult(command=current_result.command, returncode=0,
                                         stdout=current_result.stdout, stderr="")
                # For non-fixable, print guidance and let the original result stand
                console.print(f"  [warning]This issue can't be auto-fixed — see above.[/warning]")
                return None  # Let the LLM try if configured
        return None
```

### 4d. Increase diagnosis context (line 343)
**Change:**
```python
error_tail = "\n".join(result.stdout.splitlines()[-60:])
```
**To:**
```python
error_tail = "\n".join(result.stdout.splitlines()[-100:])
```

### 4e. Add subproject context to diagnosis (line 345-351)
**Add after `files = self._project_signature()`:**
```python
cwd_hint = ""
if self.project_dir:
    cwd_hint = f"Working dir: {self.project_dir.name}\n"
```
**And insert `cwd_hint` into the prompt string.**

---

## Step 5: Better Windows Setup Script Awareness
**File:** `devready/engine.py`

### 5a. Modify `_try_project_setup` (around line 460-465)
**Before the existing logic, add:**
```python
    detected = strategies.detect_setup_strategies(self.project_dir)
    if not detected:
        return False

    strategy = detected[0]

    # On Windows, if the project's setup is a bash script, it's almost certainly
    # Unix-only — warn and skip rather than letting it fail.
    if sys.platform == "win32" and strategy.runner == "bash":
        console.print(
            f"  This project provides its own setup: [bold]{strategy.display}[/bold]"
        )
        console.print(
            "  [warning]The setup script is Unix-only and won't run on Windows — "
            "skipping it.[/warning]\n"
            "  [muted]DevReady will use its own cross-platform setup instead.[/muted]"
        )
        return False
```

---

## Step 6: Subproject Prioritization
**File:** `devready/engine.py`

### 6a. Track root setup outcome per language (after `_step_environment`)
**Add a new instance attribute in `__init__` (line 66 area):**
```python
self._failed_languages: set[str] = set()  # languages whose root setup failed
```

### 6b. Record failures in `_step_environment` (around line 356-358)
**After the outcome check:**
```python
                if outcomes:
                    self._install_ok = self._install_ok and all(o.ok for o in outcomes)
                    if det.language and not all(o.ok for o in outcomes):
                        self._failed_languages.add(det.language)
```

### 6c. Modify `_setup_subprojects` (around line 418-425)
**After the subproject loop start, before asking:**
```python
    for subdir, results in subprojects:
        rel = subdir.relative_to(self.project_dir).as_posix()
        langs = ", ".join(r.language for r in results)
        # Skip subproject if its language already failed in the root — it would
        # fail again and waste time (e.g. Rust/Tauri in a Node.js repo).
        if any(r.language in self._failed_languages for r in results):
            console.print(
                f"    [muted]Skipping {rel} ({langs}) — the same language failed at "
                f"the root level.[/muted]"
            )
            continue
        if not self._confirm(f"    Set up [bold]{rel}[/bold] ({langs})? [Y/n] "):
```

---

## Step 7: Code-Level Fixes

### 7a. `env_vars.py` — Change token generation (line 62)
**Change:**
```python
return secrets.token_urlsafe(32)
```
**To:**
```python
return secrets.token_hex(32)
```

### 7b. `system_deps.py` — Prefer rustup over rust (line 175)
**Change:**
```python
"cargo": {  # Rust (rustup provides cargo)
    "choco": "rust", "scoop": "rust", "winget": "Rustlang.Rustup",
```
**To:**
```python
"cargo": {  # Rust (rustup provides cargo)
    "choco": "rust", "scoop": "rustup", "winget": "Rustlang.Rustup",
```

### 7c. `healer.py` — Add Windows exe variants to forbidden tokens (line 56-61)
**After `_FORBIDDEN_TOKENS` tuple, or modify the safety check:**
The `_FORBIDDEN_TOKENS` already includes `curl`, `iwr`, `irm`. The existing check at line 469 uses `any(token in low for token in _FORBIDDEN_TOKENS)` which catches substrings — so `curl.exe` would contain `curl` and be caught already. No change needed here.

---

## Step 8: Test Verification

After all edits, run:
```bash
pip install -e ".[dev]"
pytest
devready doctor
```
