"""Python project detector.

Recognises Python projects by their dependency/build files and tries to extract
the required interpreter version and a few common frameworks. Kept deliberately
lightweight — full TOML parsing of pyproject is avoided so we don't add a
dependency just for best-effort hints.
"""

from __future__ import annotations

import re
from typing import List, Optional

from .base import DetectionResult, Detector

# Files that strongly indicate a Python project, in rough priority order.
PYTHON_MARKERS = [
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "environment.yml",
]

# Substrings that, if present in a dependency file, suggest a framework.
# Mapping: lowercase needle -> display name.
FRAMEWORK_HINTS = {
    "django": "Django",
    "flask": "Flask",
    "fastapi": "FastAPI",
    "celery": "Celery",
    "streamlit": "Streamlit",
    "pytest": "pytest",
}


class PythonDetector(Detector):
    """Detect Python projects and their key characteristics."""

    name = "python"

    def detect(self) -> Optional[DetectionResult]:
        matched = [m for m in PYTHON_MARKERS if self.has_file(m)]
        if not matched:
            return None

        # Gather the text of all dependency files so we can scan once for both
        # the version and the framework hints.
        blob = "\n".join(filter(None, (self.read_file(m) for m in matched))).lower()

        return DetectionResult(
            language="Python",
            frameworks=self._detect_frameworks(blob),
            version=self._detect_version(),
            package_files=matched,
            confidence=1.0,
        )

    # -- Internals -----------------------------------------------------------
    def _detect_frameworks(self, blob: str) -> List[str]:
        """Return display names of frameworks whose hint appears in the blob."""
        return [display for needle, display in FRAMEWORK_HINTS.items() if needle in blob]

    def _detect_version(self) -> Optional[str]:
        """Best-effort extraction of the required Python version.

        We check, in order:
          * ``.python-version`` (used by pyenv) — most authoritative.
          * ``requires-python`` in pyproject.toml.
          * ``python_requires`` in setup.py/setup.cfg.
        Returns a bare version like "3.11" or None if nothing is declared.
        """
        # 1. pyenv's .python-version file is the clearest signal.
        pinned = self.read_file(".python-version")
        if pinned and pinned.strip():
            return pinned.strip().splitlines()[0].strip()

        # 2. Look for a version constraint and pull out the first X.Y number.
        for filename in ("pyproject.toml", "setup.py", "setup.cfg"):
            content = self.read_file(filename)
            if not content:
                continue
            match = re.search(
                r"(?:requires-python|python_requires)\s*=\s*['\"]?[^0-9]*([0-9]+\.[0-9]+)",
                content,
            )
            if match:
                return match.group(1)
        return None
