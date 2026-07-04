"""The curated catalog — the safe, default surface of the web GUI.

Non-technical users browse and install *only* from this vetted list, which is
what keeps the "easy app" safe: they can't accidentally one-click a random or
malicious repo. (Open GitHub search is a separate, explicitly-warned path.)

The catalog ships as ``catalog.json`` next to this module. This file just loads
it and provides simple browse/search helpers — no network, no surprises.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

_CATALOG_FILE = Path(__file__).with_name("catalog.json")
_VERIFIED_FILE = Path(__file__).with_name("catalog_verified.json")


@lru_cache(maxsize=1)
def _load() -> Dict:
    """Load and cache the catalog JSON (read once per process)."""
    data = json.loads(_CATALOG_FILE.read_text(encoding="utf-8"))
    verified = _load_verified()
    projects = [
        {**p, "verified": verified.get(p.get("id", ""), {})}
        for p in data.get("projects", [])
    ]
    return {
        "categories": data.get("categories", []),
        "projects": projects,
    }


def _load_verified() -> Dict:
    """Per-app, per-OS install verification results from the nightly dogfood CI.

    Shape: ``{app_id: {os_key: {"ok": bool, "checked_at": "YYYY-MM-DD"}}}``.
    Missing file (fresh checkout, or CI hasn't run yet) means no badges.
    """
    try:
        return json.loads(_VERIFIED_FILE.read_text(encoding="utf-8")).get("apps", {})
    except (OSError, json.JSONDecodeError):
        return {}


def merge_verified_results(existing: Dict, run_payload: Dict) -> Dict:
    """Fold one OS's nightly run into the accumulated verification file.

    ``run_payload`` is what scripts/verify_catalog.py writes:
    ``{"os": "windows", "checked_at": "…", "results": {app_id: {"ok": …}}}``.
    Other OSes' entries are preserved; this OS's entries are replaced.
    """
    apps = dict(existing.get("apps", {}))
    os_key = run_payload.get("os", "unknown")
    for app_id, result in (run_payload.get("results") or {}).items():
        entry = dict(apps.get(app_id, {}))
        entry[os_key] = {
            "ok": bool(result.get("ok")),
            "checked_at": run_payload.get("checked_at", ""),
            "seconds": result.get("seconds"),
        }
        apps[app_id] = entry
    return {"apps": apps}


def categories() -> List[Dict]:
    """Return the category definitions (id/label/icon) for the GUI's filters."""
    return list(_load()["categories"])


def all_projects() -> List[Dict]:
    """Return every catalog project."""
    return list(_load()["projects"])


def get_project(project_id: str) -> Optional[Dict]:
    """Return a single catalog project by id, or None if not found."""
    for project in _load()["projects"]:
        if project.get("id") == project_id:
            return project
    return None


def search(query: str = "", category: str = "") -> List[Dict]:
    """Filter catalog projects by free-text ``query`` and/or ``category``.

    Matching is case-insensitive and spans name, description, language, and
    tags so a search like "chat" or "python" finds the obvious results.
    """
    query = (query or "").strip().lower()
    category = (category or "").strip().lower()
    results = []
    for project in _load()["projects"]:
        if category and project.get("category", "").lower() != category:
            continue
        if query:
            haystack = " ".join(
                [
                    project.get("name", ""),
                    project.get("description", ""),
                    project.get("language", ""),
                    " ".join(project.get("tags", [])),
                ]
            ).lower()
            if query not in haystack:
                continue
        results.append(project)
    return results


def is_known_repo(repo_url: str) -> bool:
    """True if ``repo_url`` matches a catalog entry (i.e. is a vetted project).

    Used by the install flow to tell a safe, curated install apart from an
    "advanced" arbitrary-URL install (which the GUI flags with a warning).
    """
    normalized = (repo_url or "").rstrip("/").lower()
    for project in _load()["projects"]:
        if project.get("repo", "").rstrip("/").lower() == normalized:
            return True
    return False
