"""Java project detector.

Recognises Maven (``pom.xml``) and Gradle (``build.gradle`` /
``build.gradle.kts``) projects, and extracts the required Java version and
common frameworks (Spring Boot, Quarkus, Micronaut).
"""

from __future__ import annotations

import re
from typing import List, Optional

from .base import DetectionResult, Detector

MAVEN_FILES = ("pom.xml",)
GRADLE_FILES = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")

# Dependency/plugin substring -> framework display name.
FRAMEWORK_HINTS = {
    "spring-boot": "Spring Boot",
    "quarkus": "Quarkus",
    "micronaut": "Micronaut",
}


class JavaDetector(Detector):
    """Detect Java projects (Maven or Gradle) and their key characteristics."""

    name = "java"

    def detect(self) -> Optional[DetectionResult]:
        matched = [f for f in (*MAVEN_FILES, *GRADLE_FILES) if self.has_file(f)]
        if not matched:
            return None

        blob = "\n".join(filter(None, (self.read_file(f) for f in matched))).lower()
        return DetectionResult(
            language="Java",
            frameworks=[d for n, d in FRAMEWORK_HINTS.items() if n in blob],
            version=self._detect_version(),
            package_files=matched,
        )

    def _detect_version(self) -> Optional[str]:
        """Best-effort Java version from .java-version, pom.xml, or Gradle."""
        pinned = self.read_file(".java-version")
        if pinned and pinned.strip():
            # e.g. "17" or "temurin-17.0.2" -> first number group
            match = re.search(r"(\d+)", pinned)
            if match:
                return match.group(1)

        pom = self.read_file("pom.xml") or ""
        match = re.search(
            r"<(?:java\.version|maven\.compiler\.release|maven\.compiler\.source|release)>\s*"
            r"(\d+)",
            pom,
        )
        if match:
            return match.group(1)

        gradle = (self.read_file("build.gradle") or "") + (self.read_file("build.gradle.kts") or "")
        match = re.search(r"(?:VERSION_|JavaLanguageVersion\.of\(|sourceCompatibility\D+)(\d+)", gradle)
        return match.group(1) if match else None
