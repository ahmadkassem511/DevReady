"""Detector registry and the top-level :func:`detect_stack` entry point.

The rest of the application calls :func:`detect_stack` and never touches the
individual detector classes directly. To support a new language, add its
detector class to :data:`ALL_DETECTORS`.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Type

from .base import DetectionResult, Detector
from .go import GoDetector
from .node import NodeDetector
from .php import PhpDetector
from .python import PythonDetector
from .ruby import RubyDetector
from .rust import RustDetector

# The ordered list of detectors DevReady runs. Order doesn't strictly matter
# because results are sorted by confidence, but keeping the most common stacks
# first is a small readability win.
ALL_DETECTORS: List[Type[Detector]] = [
    PythonDetector,
    NodeDetector,
    GoDetector,
    RustDetector,
    RubyDetector,
    PhpDetector,
]


def detect_stack(project_dir: Path) -> List[DetectionResult]:
    """Run every detector against ``project_dir`` and collect the matches.

    Returns a list (possibly empty for an unrecognised project, or multiple
    entries for a polyglot repo such as a JS frontend + Python backend),
    sorted by descending confidence so the most likely stack comes first.
    """
    results: List[DetectionResult] = []
    for detector_cls in ALL_DETECTORS:
        result = detector_cls(project_dir).detect()
        if result is not None:
            results.append(result)

    results.sort(key=lambda r: r.confidence, reverse=True)
    return results


__all__ = ["detect_stack", "DetectionResult", "Detector", "ALL_DETECTORS"]
