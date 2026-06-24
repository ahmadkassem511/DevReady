"""Live "Popular on GitHub" discovery via the GitHub Search API.

The curated catalog (``catalog.py``) is the small, vetted, safe-by-default set.
This module is the *breadth*: it lets the Discover page browse the most-starred
public repositories by category, with real star counts and descriptions, so
users can find far more than we could ever hand-curate.

We use GitHub's public Search API (no token needed for light use; set the
``GITHUB_TOKEN`` env var to raise the rate limit). Results are mapped to the same
shape the GUI already uses for catalog projects, plus ``stars``.
"""

from __future__ import annotations

import os
from typing import Dict, List, Tuple

import httpx

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"

# Discover categories shown as chips in the GUI. "featured" is special (served
# from the curated catalog); the rest map to a GitHub topic query that surfaces
# the most-starred repos in that space.
DISCOVER_CATEGORIES: List[Dict] = [
    {"id": "featured", "label": "Featured"},
    {"id": "ai", "label": "AI & LLMs", "query": "topic:machine-learning"},
    {"id": "web", "label": "Web Apps", "query": "topic:web"},
    {"id": "data", "label": "Data & Tools", "query": "topic:developer-tools"},
    {"id": "media", "label": "Media & Creative", "query": "topic:multimedia"},
    {"id": "devtools", "label": "Dev Tools", "query": "topic:cli"},
    {"id": "games", "label": "Games", "query": "topic:game"},
]

_CATEGORY_QUERIES = {c["id"]: c.get("query", "") for c in DISCOVER_CATEGORIES}


def build_query(text: str = "", category: str = "") -> str:
    """Build a GitHub search query string from a text term and/or category.

    Always constrains to repos with a meaningful star count so the results are
    real, popular projects rather than noise. Defaults to "most-starred overall"
    when neither a term nor a category is given.
    """
    parts: List[str] = []
    text = (text or "").strip()
    if text:
        parts.append(text)
    cat_query = _CATEGORY_QUERIES.get((category or "").strip())
    if cat_query:
        parts.append(cat_query)
    # Floor on stars keeps results to genuinely popular projects.
    parts.append("stars:>500" if (text or cat_query) else "stars:>20000")
    return " ".join(parts)


def _headers() -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "DevReady"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def search_repositories(
    text: str = "", category: str = "", page: int = 1, per_page: int = 30
) -> Tuple[List[Dict], str]:
    """Return ``(projects, error)`` for the most-starred repos matching the query.

    ``error`` is an empty string on success, or a friendly message (rate limit /
    network) that the GUI can show. Each project dict matches the catalog shape
    plus ``stars``, ``html_url`` and ``topics``.
    """
    params = {
        "q": build_query(text, category),
        "sort": "stars",
        "order": "desc",
        "per_page": max(1, min(per_page, 100)),
        "page": max(1, page),
    }
    try:
        resp = httpx.get(GITHUB_SEARCH_URL, params=params, headers=_headers(), timeout=20)
    except httpx.HTTPError:
        return [], "Couldn't reach GitHub. Check your internet connection and try again."

    if resp.status_code == 403:
        return [], "GitHub's hourly search limit was hit. Try again in a minute (or set a GITHUB_TOKEN)."
    if resp.status_code != 200:
        return [], f"GitHub search failed ({resp.status_code}). Try again shortly."

    items = resp.json().get("items", [])
    return [_map_repo(it) for it in items], ""


def _map_repo(item: Dict) -> Dict:
    """Map a GitHub API repo object to DevReady's project shape."""
    return {
        "id": item.get("full_name", ""),
        "name": item.get("name", ""),
        "full_name": item.get("full_name", ""),
        "repo": item.get("clone_url") or f"{item.get('html_url', '')}.git",
        "html_url": item.get("html_url", ""),
        "stars": item.get("stargazers_count", 0),
        "language": item.get("language") or "",
        "description": item.get("description") or "",
        "topics": item.get("topics", [])[:5],
    }
