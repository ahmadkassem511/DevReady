""".NET project detector.

Recognises .NET projects via project/solution files (``*.csproj``, ``*.fsproj``,
``*.sln``) and extracts the target framework version and whether it's an
ASP.NET Core web app.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional

from .base import DetectionResult, Detector


class DotnetDetector(Detector):
    """Detect .NET projects and their key characteristics."""

    name = "dotnet"

    def detect(self) -> Optional[DetectionResult]:
        # Project files can sit at the root or one level down (common layout).
        project_files = self._find_project_files()
        if not project_files:
            return None

        blob = "\n".join(self._safe_read(p) for p in project_files)
        frameworks = []
        # ASP.NET projects use the Web SDK or reference AspNetCore packages.
        if "Microsoft.NET.Sdk.Web" in blob or "Microsoft.AspNetCore" in blob:
            frameworks.append("ASP.NET Core")

        rel_files = [p.relative_to(self.project_dir).as_posix() for p in project_files]
        return DetectionResult(
            language=".NET",
            frameworks=frameworks,
            version=self._detect_version(blob),
            package_files=rel_files[:4],  # keep the summary tidy
        )

    def _find_project_files(self) -> List[Path]:
        """Locate .csproj/.fsproj/.sln files at the root or one level deep."""
        found: List[Path] = []
        for pattern in ("*.csproj", "*.fsproj", "*.sln", "*/*.csproj", "*/*.fsproj"):
            found.extend(sorted(self.project_dir.glob(pattern)))
        return found

    def _safe_read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""

    def _detect_version(self, blob: str) -> Optional[str]:
        """Target framework from <TargetFramework> or the SDK in global.json."""
        # e.g. <TargetFramework>net8.0</TargetFramework> -> 8.0
        match = re.search(r"net(?:coreapp)?(\d+\.\d+)", blob)
        if match:
            return match.group(1)

        global_json = self.read_file("global.json")
        if global_json:
            try:
                version = json.loads(global_json).get("sdk", {}).get("version", "")
            except json.JSONDecodeError:
                version = ""
            m = re.match(r"(\d+\.\d+)", version)
            if m:
                return m.group(1)
        return None
