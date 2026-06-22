"""Node.js project detector.

Recognises Node projects via ``package.json`` and extracts the required Node
version (from ``engines.node`` or an ``.nvmrc`` file) plus common frameworks
inferred from the declared dependencies.
"""

from __future__ import annotations

import json
import re
from typing import List, Optional

from .base import DetectionResult, Detector

# Dependency name -> framework display name. We check both dependencies and
# devDependencies for these.
FRAMEWORK_HINTS = {
    "next": "Next.js",
    "react": "React",
    "vue": "Vue",
    "@angular/core": "Angular",
    "express": "Express",
    "nestjs": "NestJS",
    "@nestjs/core": "NestJS",
    "svelte": "Svelte",
}


class NodeDetector(Detector):
    """Detect Node.js projects and their key characteristics."""

    name = "node"

    def detect(self) -> Optional[DetectionResult]:
        if not self.has_file("package.json"):
            return None

        raw = self.read_file("package.json")
        package = self._safe_parse(raw)

        return DetectionResult(
            language="Node.js",
            frameworks=self._detect_frameworks(package),
            version=self._detect_version(package),
            package_files=["package.json"],
            confidence=1.0,
        )

    # -- Internals -----------------------------------------------------------
    @staticmethod
    def _safe_parse(raw: Optional[str]) -> dict:
        """Parse package.json, tolerating a missing or malformed file."""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _detect_frameworks(self, package: dict) -> List[str]:
        """Look through (dev)dependencies for known frameworks."""
        deps = {}
        deps.update(package.get("dependencies", {}))
        deps.update(package.get("devDependencies", {}))
        found = [display for dep, display in FRAMEWORK_HINTS.items() if dep in deps]
        # De-duplicate while preserving order (e.g. NestJS has two hint keys).
        seen: List[str] = []
        for name in found:
            if name not in seen:
                seen.append(name)
        return seen

    def _detect_version(self, package: dict) -> Optional[str]:
        """Extract the required Node version from engines.node or .nvmrc."""
        # 1. .nvmrc is the most explicit signal and is what nvm reads.
        nvmrc = self.read_file(".nvmrc")
        if nvmrc and nvmrc.strip():
            return nvmrc.strip().lstrip("v").splitlines()[0].strip()

        # 2. Fall back to the "engines" field, pulling out the first number.
        engines = package.get("engines", {})
        node_spec = engines.get("node") if isinstance(engines, dict) else None
        if node_spec:
            match = re.search(r"([0-9]+(?:\.[0-9]+)?)", node_spec)
            if match:
                return match.group(1)
        return None
