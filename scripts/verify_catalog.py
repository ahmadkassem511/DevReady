"""Dogfood the catalog: install every flagged app with DevReady itself.

Run nightly by .github/workflows/verify-catalog.yml on real Windows/macOS/
Linux runners. For each catalog project marked ``"verify": true`` it does what
a user does — clone, ``devready start --yes`` — and records whether that
succeeded, per OS. The merged results power the "✓ Verified installs on
Windows" badges in the GUI, and every failure is a real regression signal.

Usage:  python scripts/verify_catalog.py [output.json]

Always exits 0 — this JOB records reality; the publish step turns it into
badges. A red X on a badge is information, not a broken build.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from devready.utils import force_rmtree
from devready.web import catalog

_OS_KEY = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}


def _run_devready(verb: str, target: Path, log_path: Path, timeout: int) -> int:
    """Run `devready <verb> <target>` with output to a FILE (never a pipe —
    the launched app inherits the handle and a pipe would make us wait on a
    server that intentionally keeps running)."""
    with open(log_path, "a", encoding="utf-8", errors="replace") as log:
        proc = subprocess.run(
            [sys.executable, "-m", "devready", verb, str(target), *(["--yes"] if verb == "start" else [])],
            stdout=log, stderr=subprocess.STDOUT, timeout=timeout,
        )
    return proc.returncode


def verify_app(app: dict, base: Path, timeout: int) -> dict:
    started = time.time()
    target = base / app["id"]
    log_path = base / f"{app['id']}.log"

    clone = subprocess.run(
        ["git", "clone", "--depth", "1", app["repo"], str(target)],
        capture_output=True, text=True, timeout=900,
    )
    if clone.returncode != 0:
        return {"ok": False, "stage": "clone", "seconds": round(time.time() - started),
                "detail": (clone.stderr or "clone failed").strip()[-400:]}

    try:
        code = _run_devready("start", target, log_path, timeout)
        ok, detail = code == 0, ""
        if not ok:
            try:
                tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                detail = "\n".join(tail[-12:])[-800:]
            except OSError:
                detail = f"exit code {code}"
    except subprocess.TimeoutExpired:
        ok, detail = False, f"timed out after {timeout}s"

    # Best-effort teardown so the runner doesn't accumulate servers/containers.
    try:
        _run_devready("stop", target, log_path, 180)
    except Exception:
        pass
    force_rmtree(target)

    return {"ok": ok, "stage": "install", "seconds": round(time.time() - started),
            "detail": "" if ok else detail}


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("verify-results.json")
    timeout = int(os.environ.get("VERIFY_APP_TIMEOUT", "1800"))
    apps = [p for p in catalog.all_projects() if p.get("verify")]
    # VERIFY_ONLY=fastapi,streamlit — run a subset (local testing / CI debugging).
    only = {s.strip() for s in os.environ.get("VERIFY_ONLY", "").split(",") if s.strip()}
    if only:
        apps = [p for p in apps if p["id"] in only]
    base = Path(tempfile.mkdtemp(prefix="devready-verify-"))

    results = {}
    for app in apps:
        print(f"::group::{app['id']}", flush=True)
        result = verify_app(app, base, timeout)
        results[app["id"]] = result
        mark = "OK" if result["ok"] else "FAIL"
        print(f"{mark} {app['id']} in {result['seconds']}s", flush=True)
        if result.get("detail"):
            print(result["detail"], flush=True)
        print("::endgroup::", flush=True)

    payload = {
        "os": _OS_KEY.get(platform.system(), platform.system().lower()),
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "results": results,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    passed = sum(1 for r in results.values() if r["ok"])
    print(f"\n{passed}/{len(results)} catalog apps verified on {payload['os']}")
    return 0  # recording, not gating — badges carry the verdict


if __name__ == "__main__":
    raise SystemExit(main())
