"""Detector framework — figuring out what kind of project we're looking at.

A *detector* inspects a project directory and decides whether it recognises the
stack (Python, Node, …). Each concrete detector lives in its own module
(``python.py``, ``node.py``) and subclasses :class:`Detector`.

Adding support for a new stack is intentionally simple:

    1. Create ``detectors/<stack>.py``.
    2. Subclass :class:`Detector`, implement ``detect()``.
    3. Register the class in ``detectors/__init__.py``'s ``ALL_DETECTORS`` list.

The detector returns a :class:`DetectionResult`, a small, serialisable record
the rest of the pipeline (engine, environment setup) reads from.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class DetectionResult:
    """Everything a detector learned about a project.

    Attributes:
        language: Human-readable language name, e.g. "Python" or "Node.js".
        frameworks: Detected frameworks/libraries, e.g. ["Django", "Celery"].
        version: Required runtime version if we could find one (e.g. "3.11").
        package_files: The dependency/build files we matched on, relative to
            the project root (e.g. ["requirements.txt"]). Useful for display
            and for the install step.
        confidence: 0.0–1.0 hint of how sure we are. The engine uses this to
            order results when multiple detectors match (polyglot repos).
    """

    language: str
    frameworks: List[str] = field(default_factory=list)
    version: Optional[str] = None
    package_files: List[str] = field(default_factory=list)
    confidence: float = 1.0


class Detector(abc.ABC):
    """Base class for all stack detectors.

    Subclasses only need to implement :meth:`detect`. The helper methods below
    keep that implementation short and consistent.
    """

    #: Short name used in logs and registration, e.g. "python".
    name: str = "base"

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir

    # -- Helpers available to subclasses ------------------------------------
    def has_file(self, *names: str) -> bool:
        """Return True if any of the given files exists in the project root."""
        return any((self.project_dir / n).exists() for n in names)

    def read_file(self, name: str) -> Optional[str]:
        """Read a project file as text, or return None if missing/unreadable."""
        path = self.project_dir / name
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    # -- The one method subclasses must implement ---------------------------
    @abc.abstractmethod
    def detect(self) -> Optional[DetectionResult]:
        """Inspect the project and return a result, or None if not a match."""
        raise NotImplementedError
