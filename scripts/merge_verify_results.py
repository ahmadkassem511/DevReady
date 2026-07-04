"""Merge per-OS nightly verify runs into devready/web/catalog_verified.json.

Usage:  python scripts/merge_verify_results.py run1.json run2.json …

Each input is one OS's output from scripts/verify_catalog.py. Existing results
for OSes not present in the inputs are preserved (a runner outage must not
erase last night's verdicts for that OS).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from devready.web.catalog import merge_verified_results

_OUT = Path(__file__).resolve().parents[1] / "devready" / "web" / "catalog_verified.json"


def main() -> int:
    try:
        merged = json.loads(_OUT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        merged = {"apps": {}}

    for arg in sys.argv[1:]:
        payload = json.loads(Path(arg).read_text(encoding="utf-8"))
        merged = merge_verified_results(merged, payload)
        print(f"merged {arg} ({payload.get('os')}, {len(payload.get('results', {}))} apps)")

    _OUT.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
