"""Ruby project detector.

Recognises Ruby projects via ``Gemfile`` and extracts the required Ruby version
(from ``.ruby-version`` or a ``ruby "x.y"`` line) and common frameworks.
"""

from __future__ import annotations

import re
from typing import Optional

from .base import DetectionResult, Detector

# Gem name -> framework display name.
FRAMEWORK_HINTS = {
    "rails": "Rails",
    "sinatra": "Sinatra",
    "hanami": "Hanami",
    "rack": "Rack",
}


class RubyDetector(Detector):
    """Detect Ruby projects and their key characteristics."""

    name = "ruby"

    def detect(self) -> Optional[DetectionResult]:
        if not self.has_file("Gemfile"):
            return None

        gemfile = self.read_file("Gemfile") or ""
        lower = gemfile.lower()
        return DetectionResult(
            language="Ruby",
            # Match on word boundaries so "rails" doesn't also flag "railties".
            frameworks=[d for g, d in FRAMEWORK_HINTS.items() if re.search(rf"\b{g}\b", lower)],
            version=self._detect_version(gemfile),
            package_files=["Gemfile"],
        )

    def _detect_version(self, gemfile: str) -> Optional[str]:
        """Find the required Ruby version from .ruby-version or the Gemfile."""
        pinned = self.read_file(".ruby-version")
        if pinned and pinned.strip():
            return pinned.strip().splitlines()[0].strip()
        match = re.search(r"""ruby\s+['"]([0-9.]+)['"]""", gemfile)
        return match.group(1) if match else None
