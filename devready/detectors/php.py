"""PHP project detector.

Recognises PHP projects via ``composer.json`` and extracts the required PHP
version (``require.php``) and common frameworks from the declared dependencies.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from .base import DetectionResult, Detector

# Composer package substring -> framework display name.
FRAMEWORK_HINTS = {
    "laravel/framework": "Laravel",
    "symfony/symfony": "Symfony",
    "symfony/framework-bundle": "Symfony",
    "slim/slim": "Slim",
    "cakephp/cakephp": "CakePHP",
}


class PhpDetector(Detector):
    """Detect PHP projects and their key characteristics."""

    name = "php"

    def detect(self) -> Optional[DetectionResult]:
        if not self.has_file("composer.json"):
            return None

        raw = self.read_file("composer.json") or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}

        require = {}
        require.update(data.get("require", {}))
        require.update(data.get("require-dev", {}))

        frameworks = []
        for pkg, display in FRAMEWORK_HINTS.items():
            if pkg in require and display not in frameworks:
                frameworks.append(display)

        return DetectionResult(
            language="PHP",
            frameworks=frameworks,
            version=self._detect_version(require.get("php")),
            package_files=["composer.json"],
        )

    @staticmethod
    def _detect_version(php_constraint: Optional[str]) -> Optional[str]:
        """Pull the first X.Y from a composer PHP constraint like '^8.1'."""
        if not php_constraint:
            return None
        match = re.search(r"([0-9]+\.[0-9]+)", php_constraint)
        return match.group(1) if match else None
