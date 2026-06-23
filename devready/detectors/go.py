"""Go project detector.

Recognises Go modules via ``go.mod`` and extracts the required Go version (the
``go 1.x`` directive) and common web frameworks from the module requirements.
"""

from __future__ import annotations

import re
from typing import Optional

from .base import DetectionResult, Detector

# Module path substring -> framework display name.
FRAMEWORK_HINTS = {
    "gin-gonic/gin": "Gin",
    "labstack/echo": "Echo",
    "gofiber/fiber": "Fiber",
    "go-chi/chi": "chi",
    "gorilla/mux": "Gorilla",
}


class GoDetector(Detector):
    """Detect Go projects and their key characteristics."""

    name = "go"

    def detect(self) -> Optional[DetectionResult]:
        if not self.has_file("go.mod"):
            return None

        gomod = self.read_file("go.mod") or ""
        version_match = re.search(r"^go\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)", gomod, re.MULTILINE)
        return DetectionResult(
            language="Go",
            frameworks=[d for n, d in FRAMEWORK_HINTS.items() if n in gomod],
            version=version_match.group(1) if version_match else None,
            package_files=["go.mod"],
        )
