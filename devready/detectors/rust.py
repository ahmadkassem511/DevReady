"""Rust project detector.

Recognises Rust projects via ``Cargo.toml`` and extracts the edition/toolchain
version and a few common web frameworks from the declared dependencies.
"""

from __future__ import annotations

import re
from typing import List, Optional

from .base import DetectionResult, Detector

# Crate name substring -> framework display name.
FRAMEWORK_HINTS = {
    "actix-web": "Actix Web",
    "rocket": "Rocket",
    "axum": "Axum",
    "warp": "Warp",
    "tokio": "Tokio",
}


class RustDetector(Detector):
    """Detect Rust projects and their key characteristics."""

    name = "rust"

    def detect(self) -> Optional[DetectionResult]:
        if not self.has_file("Cargo.toml"):
            return None

        cargo = self.read_file("Cargo.toml") or ""
        return DetectionResult(
            language="Rust",
            frameworks=[d for n, d in FRAMEWORK_HINTS.items() if n in cargo.lower()],
            version=self._detect_version(cargo),
            package_files=["Cargo.toml"],
        )

    def _detect_version(self, cargo: str) -> Optional[str]:
        """Find the required Rust version from rust-toolchain or Cargo.toml."""
        # rust-toolchain(.toml) pins the toolchain most explicitly.
        toolchain = self.read_file("rust-toolchain.toml") or self.read_file("rust-toolchain") or ""
        match = re.search(r'channel\s*=\s*"([0-9.]+)"', toolchain)
        if match:
            return match.group(1)
        # Otherwise honour an explicit rust-version in Cargo.toml.
        match = re.search(r'rust-version\s*=\s*"([0-9.]+)"', cargo)
        return match.group(1) if match else None
