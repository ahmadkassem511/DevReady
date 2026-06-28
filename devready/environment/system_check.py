"""Pre-install system compatibility check.

Detects hardware specs, reads the project's README for requirements (LLM +
regex fallback), compares them, and produces a compatibility report. Runs as
Step 3 of the pipeline (after detection + README analysis, before any install).
On critical mismatches the installation is blocked with a clear explanation.
"""

from __future__ import annotations

import csv
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..utils import command_exists, console


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class HardwareInfo:
    """What the user's machine has."""
    os_name: str           # "Windows 10", "macOS 14.6", "Ubuntu 22.04"
    os_arch: str           # "x86_64", "arm64", "aarch64"
    cpu_cores: int
    cpu_model: str         # processor brand string
    ram_gb: float          # total physical RAM
    disk_free_gb: float    # free space on the project drive
    gpu_model: Optional[str] = None
    gpu_vram_gb: Optional[float] = None
    gpu_cuda_capable: bool = False


@dataclass
class SystemRequirements:
    """Minimum and recommended hardware for the project, extracted from the README."""
    os_names: List[str] = field(default_factory=list)
    cpu_min_cores: Optional[int] = None
    cpu_arch: Optional[str] = None    # "x86_64", "arm64", "aarch64"
    ram_min_gb: Optional[float] = None
    disk_min_gb: Optional[float] = None
    gpu_required: bool = False
    gpu_cuda_required: bool = False
    gpu_vram_min_gb: Optional[float] = None
    notes: str = ""
    source: str = "none"    # "llm", "regex", or "none"


_CHECK_RESULT_FIELDS = ["name", "status", "current", "required", "message"]


class CheckResult:
    """One check in the compatibility report."""

    __slots__ = _CHECK_RESULT_FIELDS

    def __init__(self, name: str, status: str, current: str, required: str, message: str):
        self.name = name
        self.status = status    # "ok", "warning", "error"
        self.current = current
        self.required = required
        self.message = message


@dataclass
class CompatibilityReport:
    """Overall verdict and per-check results."""
    compatible: bool
    checks: List[CheckResult] = field(default_factory=list)
    hw: Optional[HardwareInfo] = None
    req: Optional[SystemRequirements] = None

    @property
    def has_errors(self) -> bool:
        return any(c.status == "error" for c in self.checks)


# ---------------------------------------------------------------------------
# Hardware detection
# ---------------------------------------------------------------------------

def get_hardware_info(project_dir: Path = Path.cwd()) -> HardwareInfo:
    """Collect the user's hardware specs using only stdlib and subprocess.

    GPU detection is best-effort and never raises — it returns None for both
    fields when it can't find anything.
    """
    # OS
    system = platform.system()
    release = platform.release()
    version = platform.version()
    os_name = f"{system} {release}" if system == "Windows" else f"{system} {release}"
    os_arch = platform.machine().lower()

    # CPU
    cpu_cores = os.cpu_count() or 1
    cpu_model = _detect_cpu_model()
    if not cpu_model:
        cpu_model = platform.processor() or "Unknown"

    # RAM
    ram_gb = _detect_ram()

    # Disk
    try:
        usage = shutil.disk_usage(project_dir)
        disk_free_gb = usage.free / (1024 ** 3)
    except OSError:
        disk_free_gb = 0.0

    # GPU (best-effort)
    gpu_model, gpu_vram, gpu_cuda = _detect_gpu()

    return HardwareInfo(
        os_name=os_name,
        os_arch=os_arch,
        cpu_cores=cpu_cores,
        cpu_model=cpu_model,
        ram_gb=ram_gb,
        disk_free_gb=disk_free_gb,
        gpu_model=gpu_model,
        gpu_vram_gb=gpu_vram,
        gpu_cuda_capable=gpu_cuda,
    )


def _detect_cpu_model() -> str:
    """Return the CPU brand string, or empty string."""
    system = platform.system()
    try:
        if system == "Windows":
            result = subprocess.run(
                ["wmic", "cpu", "get", "name"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if line and "Name" not in line:
                    return line
        elif system == "Linux":
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        elif system == "Darwin":
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _detect_ram() -> float:
    """Return total physical RAM in GB."""
    system = platform.system()
    try:
        if system == "Windows":
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            mem = MEMORYSTATUSEX()
            mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
            return mem.ullTotalPhys / (1024 ** 3)
        elif system == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return kb / (1024 * 1024)
        elif system == "Darwin":
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5,
            )
            return int(result.stdout.strip()) / (1024 ** 3)
    except Exception:
        pass
    return 0.0


def _detect_gpu() -> Tuple[Optional[str], Optional[float], bool]:
    """Detect GPU model, VRAM, and CUDA capability.

    Returns ``(model, vram_gb, cuda_capable)`` or ``(None, None, False)``.
    """
    system = platform.system()
    try:
        if system == "Windows":
            result = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get", "Name,AdapterRAM", "/format:csv"],
                capture_output=True, text=True, timeout=5,
            )
            reader = csv.DictReader([l for l in result.stdout.splitlines() if l.strip()])
            for row in reader:
                name = row.get("Name", "").strip()
                ram_str = row.get("AdapterRAM", "").strip()
                if not name:
                    continue
                model = name
                try:
                    vram = int(ram_str) / (1024 ** 3)
                except (ValueError, TypeError):
                    vram = None
                return model, vram, _gpu_is_cuda_capable(model)
        elif system == "Linux":
            if command_exists("nvidia-smi"):
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=name,memory.total",
                     "--format=csv,noheader"],
                    capture_output=True, text=True, timeout=10,
                )
                for line in result.stdout.splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) == 2:
                        model = parts[0]
                        try:
                            vram = float(parts[1].replace(" MiB", "").strip()) / 1024
                        except ValueError:
                            vram = None
                        return model, vram, True  # nvidia-smi exists → CUDA-capable
            # No nvidia-smi — try lspci for the model name
            result = subprocess.run(
                ["lspci"], capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                if "VGA" in line or "3D" in line:
                    model = line.split(":", 2)[-1].strip() if ":" in line else line
                    return model, None, _gpu_is_cuda_capable(model)
        elif system == "Darwin":
            result = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True, text=True, timeout=15,
            )
            model = None
            vram = None
            for line in result.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("Chipset Model:"):
                    model = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("VRAM "):
                    vram_str = stripped.split(":", 1)[1].strip()
                    try:
                        vram = float(vram_str.replace(" GB", "").replace(" MB", ""))
                        if "MB" in vram_str:
                            vram /= 1024
                    except ValueError:
                        vram = None
            if model:
                return model, vram, _gpu_is_cuda_capable(model)
    except Exception:
        pass
    return None, None, False


def _gpu_is_cuda_capable(model: str) -> bool:
    """Check whether a GPU model string indicates CUDA support.

    CUDA is NVIDIA-only. Intel integrated GPUs and most AMD GPUs do not support
    CUDA (AMD uses ROCm). Returns False when the model is unknown / ambiguous.
    """
    if not model:
        return False
    lower = model.lower()
    # NVIDIA models are CUDA-capable.
    if "nvidia" in lower:
        return True
    # Known non-CUDA architectures.
    if "intel" in lower or "amd" in lower or "apple" in lower or "microsoft" in lower:
        return False
    # Best-effort: if we can't tell, assume not CUDA-capable.
    return False


# ---------------------------------------------------------------------------
# Requirements extraction
# ---------------------------------------------------------------------------

_REQUIREMENTS_SYSTEM_PROMPT = (
    "You are a hardware-requirements analyst. Given a README excerpt and project "
    "facts, determine what hardware this project needs to run comfortably. "
    "Return ONLY a JSON object (no markdown, no prose) with exactly these keys:\n"
    '  "os": array of operating systems the project supports '
    '(e.g. ["Windows", "macOS", "Linux"]), or [] if unknown,\n'
    '  "cpu_min_cores": minimum CPU cores required, or null,\n'
    '  "cpu_arch": required CPU architecture if specified ("x86_64", "arm64", '
    '"aarch64"), or null,\n'
    '  "ram_min_gb": minimum RAM in GB, or null,\n'
    '  "disk_min_gb": minimum free disk space in GB, or null,\n'
    '  "gpu_required": boolean — true only if the project explicitly requires a '
    "GPU (CUDA, Metal, etc.),\n"
    '  "gpu_cuda_required": boolean — true only if the project requires a '
    "CUDA-capable NVIDIA GPU specifically,\n"
    '  "gpu_vram_min_gb": minimum GPU VRAM in GB if specified, or null,\n'
    '  "notes": one short sentence with any other relevant hardware notes, '
    'or "" if none.\n'
    "Use null or empty arrays when the README doesn't mention specific "
    "hardware requirements."
)


def extract_requirements(
    readme_text: str,
    config: Optional["Config"] = None,
    detections: Optional[List] = None,
) -> SystemRequirements:
    """Extract hardware requirements from README using LLM then regex fallback.

    ``config`` is needed for the LLM path (``Config`` from ``devready.config``).
    ``detections`` is a ``List[DetectionResult]`` with language/framework info.
    """
    llm_result: Optional[dict] = None
    if config is not None and config.llm.is_configured and readme_text.strip():
        llm_result = _extract_llm(readme_text, config, detections)
        if llm_result is not None:
            req = _dict_to_requirements(llm_result)
            req.source = "llm"
            return req

    regex_result = _extract_regex(readme_text)
    if regex_result is not None:
        regex_result.source = "regex"
        return regex_result

    return SystemRequirements(source="none")


def _extract_llm(
    readme_text: str,
    config: "Config",
    detections: Optional[List] = None,
) -> Optional[dict]:
    """Ask the LLM for hardware requirements. Returns parsed dict or None."""
    from ..ai.client import ask_llm_json

    langs = ", ".join(sorted({d.language for d in detections})) if detections else "unknown"
    frameworks = ", ".join(sorted({f for d in detections for f in d.frameworks})) if detections else "none"

    excerpt = readme_text[:8000]
    user_prompt = (
        f"Project languages: {langs}\n"
        f"Frameworks: {frameworks}\n\n"
        f"README excerpt:\n{excerpt}\n"
    )
    return ask_llm_json(config, _REQUIREMENTS_SYSTEM_PROMPT, user_prompt)


def _extract_regex(readme_text: str) -> Optional[SystemRequirements]:
    """Scan README text for hardware requirement patterns. Best-effort."""
    if not readme_text.strip():
        return None

    text = readme_text.lower()
    req = SystemRequirements(source="regex")

    # OS detection
    os_map = {
        "windows": "Windows",
        "macos": "macOS",
        "mac os": "macOS",
        "linux": "Linux",
        "ubuntu": "Linux",
        "debian": "Linux",
        "centos": "Linux",
        "rhel": "Linux",
    }
    found_oses: set = set()
    for key, val in os_map.items():
        if re.search(rf"\b{re.escape(key)}\b", text, re.IGNORECASE):
            found_oses.add(val)
    if found_oses:
        req.os_names = sorted(found_oses)

    # RAM
    ram_match = re.search(
        r"(?:minimum|requires?|recommends?|needs?)\s*(?:at\s+least\s+)?"
        r"(\d+(?:\.\d+)?)\s*gb?\s*(?:of\s+)?(?:ram|memory)",
        text,
    )
    if ram_match:
        req.ram_min_gb = float(ram_match.group(1))
    else:
        ram_match = re.search(r"(\d+(?:\.\d+)?)\s*gb?\s*(?:of\s+)?(?:ram|memory)\s+(?:or\s+)?more", text)
        if ram_match:
            req.ram_min_gb = float(ram_match.group(1))

    # Disk
    disk_match = re.search(
        r"(?:requires?|minimum|needs?)\s*(?:at\s+least\s+)?"
        r"(\d+(?:\.\d+)?)\s*gb?\s*(?:of\s+)?(?:free\s+)?(?:disk|space|storage)",
        text,
    )
    if disk_match:
        req.disk_min_gb = float(disk_match.group(1))

    # CPU cores
    cores_match = re.search(
        r"(?:minimum|requires?|needs?)\s*(?:at\s+least\s+)?"
        r"(\d+)\s*(?:cpu\s+)?cores?",
        text,
    )
    if cores_match:
        req.cpu_min_cores = int(cores_match.group(1))

    # CPU architecture
    for arch in ("x86_64", "arm64", "aarch64", "amd64"):
        if arch in text:
            req.cpu_arch = arch
            break

    # GPU
    if re.search(r"(requires?\s+(a\s+)?gpu|nvidia\s+gpu)", text):
        req.gpu_required = True
    # CUDA-specific requirement
    if re.search(r"cuda", text):
        req.gpu_required = True
        req.gpu_cuda_required = True
    vram_match = re.search(
        r"(?:minimum|requires?|recommends?)\s*(?:at\s+least\s+)?"
        r"(\d+(?:\.\d+)?)\s*gb?\s*(?:of\s+)?vram",
        text,
    )
    if vram_match:
        req.gpu_vram_min_gb = float(vram_match.group(1))

    # Notes — capture requirement lines
    notes: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if any(kw in stripped for kw in ("requires", "minimum", "recommended", "need", "must have")):
            notes.append(stripped[:150])
    if notes:
        req.notes = "; ".join(notes[:3])

    # Did we find anything useful?
    has_any = (
        bool(req.os_names)
        or req.ram_min_gb is not None
        or req.disk_min_gb is not None
        or req.cpu_min_cores is not None
        or req.cpu_arch is not None
        or req.gpu_required
        or req.gpu_cuda_required
        or req.gpu_vram_min_gb is not None
    )
    if not has_any:
        return None
    return req


def _dict_to_requirements(data: dict) -> SystemRequirements:
    """Convert the JSON dict from the LLM into a SystemRequirements."""
    return SystemRequirements(
        os_names=[str(s) for s in data.get("os", []) if s],
        cpu_min_cores=_as_int(data.get("cpu_min_cores")),
        cpu_arch=str(data["cpu_arch"]) if data.get("cpu_arch") else None,
        ram_min_gb=_as_float(data.get("ram_min_gb")),
        disk_min_gb=_as_float(data.get("disk_min_gb")),
        gpu_required=bool(data.get("gpu_required")),
        gpu_cuda_required=bool(data.get("gpu_cuda_required")),
        gpu_vram_min_gb=_as_float(data.get("gpu_vram_min_gb")),
        notes=str(data.get("notes", "")).strip(),
        source="llm",
    )


def _as_int(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _as_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def check_compatibility(hw: HardwareInfo, req: SystemRequirements) -> CompatibilityReport:
    """Compare hardware against requirements and produce a compatibility report.

    Critical errors (status="error") block installation — these are things like
    wrong OS architecture or a missing required GPU. Warnings let the user
    continue but are displayed prominently.
    """
    checks: List[CheckResult] = []
    compatible = True

    # OS architecture (critical)
    if req.cpu_arch:
        hw_arch = hw.os_arch
        if hw_arch != req.cpu_arch and hw_arch.replace("aarch64", "arm64") != req.cpu_arch:
            compatible = False
            checks.append(CheckResult(
                "OS Architecture", "error",
                hw.os_arch, req.cpu_arch,
                f"This project requires {req.cpu_arch} architecture, but your system is {hw.os_arch}.",
            ))
        else:
            checks.append(CheckResult(
                "OS Architecture", "ok",
                hw.os_arch, req.cpu_arch,
                "Architecture matches.",
            ))

    # OS family (critical)
    if req.os_names:
        hw_os_lower = hw.os_name.lower()
        matched = any(os_name.lower() in hw_os_lower for os_name in req.os_names)
        if not matched:
            compatible = False
            checks.append(CheckResult(
                "Operating System", "error",
                hw.os_name, ", ".join(req.os_names),
                f"This project supports {', '.join(req.os_names)}, but you're running {hw.os_name}.",
            ))
        else:
            checks.append(CheckResult(
                "Operating System", "ok",
                hw.os_name, ", ".join(req.os_names),
                "OS is supported.",
            ))

    # CPU cores (warning)
    if req.cpu_min_cores is not None:
        if hw.cpu_cores >= req.cpu_min_cores:
            checks.append(CheckResult(
                "CPU Cores", "ok",
                str(hw.cpu_cores), str(req.cpu_min_cores),
                f"{hw.cpu_cores} cores meets the requirement of {req.cpu_min_cores}.",
            ))
        else:
            compatible = False
            checks.append(CheckResult(
                "CPU Cores", "error",
                str(hw.cpu_cores), str(req.cpu_min_cores),
                f"Only {hw.cpu_cores} cores — the project recommends at least {req.cpu_min_cores}.",
            ))

    # RAM (warning if below, fine if meets)
    if req.ram_min_gb is not None:
        if hw.ram_gb >= req.ram_min_gb:
            checks.append(CheckResult(
                "RAM", "ok",
                f"{hw.ram_gb:.1f} GB", f"{req.ram_min_gb:.0f} GB",
                f"{hw.ram_gb:.1f} GB of RAM meets the requirement of {req.ram_min_gb:.0f} GB.",
            ))
        else:
            compatible = False
            checks.append(CheckResult(
                "RAM", "error",
                f"{hw.ram_gb:.1f} GB", f"{req.ram_min_gb:.0f} GB",
                f"Only {hw.ram_gb:.1f} GB of RAM — the project requires at least {req.ram_min_gb:.0f} GB.",
            ))

    # Disk (warning if below)
    if req.disk_min_gb is not None:
        if hw.disk_free_gb >= req.disk_min_gb:
            checks.append(CheckResult(
                "Free Disk Space", "ok",
                f"{hw.disk_free_gb:.1f} GB", f"{req.disk_min_gb:.0f} GB",
                f"{hw.disk_free_gb:.1f} GB free meets the requirement.",
            ))
        else:
            checks.append(CheckResult(
                "Free Disk Space", "warning",
                f"{hw.disk_free_gb:.1f} GB", f"{req.disk_min_gb:.0f} GB",
                f"Only {hw.disk_free_gb:.1f} GB free — the project recommends {req.disk_min_gb:.0f} GB.",
            ))

    # GPU (critical if required)
    if req.gpu_required:
        if hw.gpu_model:
            # CUDA-specific check: project requires CUDA but GPU isn't CUDA-capable.
            if req.gpu_cuda_required and not hw.gpu_cuda_capable:
                compatible = False
                checks.append(CheckResult(
                    "GPU", "error",
                    f"{hw.gpu_model} (not CUDA)", "CUDA-capable GPU",
                    f"This project requires a CUDA-capable GPU, but "
                    f"{hw.gpu_model} does not support CUDA.",
                ))
            else:
                checks.append(CheckResult(
                    "GPU", "ok",
                    hw.gpu_model or "None detected", "Required",
                    f"GPU detected: {hw.gpu_model}.",
                ))
            if req.gpu_vram_min_gb is not None and hw.gpu_vram_gb is not None:
                if hw.gpu_vram_gb >= req.gpu_vram_min_gb:
                    checks.append(CheckResult(
                        "GPU VRAM", "ok",
                        f"{hw.gpu_vram_gb:.1f} GB", f"{req.gpu_vram_min_gb:.0f} GB",
                        f"{hw.gpu_vram_gb:.1f} GB VRAM meets the requirement.",
                    ))
                else:
                    checks.append(CheckResult(
                        "GPU VRAM", "warning",
                        f"{hw.gpu_vram_gb:.1f} GB", f"{req.gpu_vram_min_gb:.0f} GB",
                        f"GPU VRAM ({hw.gpu_vram_gb:.1f} GB) is below the recommended {req.gpu_vram_min_gb:.0f} GB.",
                    ))
        else:
            compatible = False
            label = "CUDA-capable GPU" if req.gpu_cuda_required else "GPU"
            detail = (
                "This project requires a CUDA-capable GPU but none was detected."
                if req.gpu_cuda_required
                else "This project requires a GPU but none was detected on your system."
            )
            checks.append(CheckResult(
                "GPU", "error",
                "Not detected", label, detail,
            ))

    # If no requirements were found, note that the system looks capable
    if not checks and not req.notes:
        checks.append(CheckResult(
            "Readiness", "ok",
            "No requirements found", "—",
            "No hardware requirements were specified — your system should be capable.",
        ))

    return CompatibilityReport(
        compatible=compatible,
        checks=checks,
        hw=hw,
        req=req,
    )


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------

def print_report(report: CompatibilityReport) -> None:
    """Print the compatibility report using Rich."""
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    # Hardware summary
    hw = report.hw
    if hw:
        hw_lines = [
            f"[bold]OS:[/bold] {hw.os_name} ({hw.os_arch})",
            f"[bold]CPU:[/bold] {hw.cpu_model} ({hw.cpu_cores} cores)",
            f"[bold]RAM:[/bold] {hw.ram_gb:.1f} GB",
            f"[bold]Free Disk:[/bold] {hw.disk_free_gb:.1f} GB",
        ]
        if hw.gpu_model:
            parts = [hw.gpu_model]
            if hw.gpu_vram_gb:
                parts.append(f"{hw.gpu_vram_gb:.1f} GB VRAM")
            if hw.gpu_cuda_capable:
                parts.append("CUDA")
            hw_lines.append(f"[bold]GPU:[/bold] {', '.join(parts)}")
        else:
            hw_lines.append("[bold]GPU:[/bold] [muted]Not detected or unavailable[/muted]")

        console.print(Panel.fit(
            "\n".join(hw_lines),
            title="[bold]System Specs[/bold]",
            border_style="cyan",
        ))

    # Requirements (if any were found)
    req = report.req
    if req and req.source != "none":
        req_lines = []
        if req.os_names:
            req_lines.append(f"[bold]OS:[/bold] {', '.join(req.os_names)}")
        if req.cpu_min_cores is not None:
            req_lines.append(f"[bold]CPU Cores:[/bold] ≥ {req.cpu_min_cores}")
        if req.cpu_arch:
            req_lines.append(f"[bold]Architecture:[/bold] {req.cpu_arch}")
        if req.ram_min_gb is not None:
            req_lines.append(f"[bold]RAM:[/bold] ≥ {req.ram_min_gb:.0f} GB")
        if req.disk_min_gb is not None:
            req_lines.append(f"[bold]Free Disk:[/bold] ≥ {req.disk_min_gb:.0f} GB")
        if req.gpu_required:
            vram = f" (≥ {req.gpu_vram_min_gb:.0f} GB VRAM)" if req.gpu_vram_min_gb else ""
            cuda = "CUDA " if req.gpu_cuda_required else ""
            req_lines.append(f"[bold]GPU:[/bold] {cuda}Required{vram}")
        if req.notes:
            req_lines.append(f"[bold]Notes:[/bold] {req.notes}")

        if req_lines:
            source_label = "AI" if req.source == "llm" else "regex"
            panel_title = f"[bold]Project Requirements[/bold] — extracted via {source_label}"
            console.print(Panel.fit(
                "\n".join(req_lines),
                title=panel_title,
                border_style="yellow",
            ))
    else:
        console.print(Panel.fit(
            "[muted]No hardware requirements were found in the README.[/muted]",
            title="[bold]Project Requirements[/bold]",
            border_style="yellow",
        ))

    # Compatibility checks table
    if report.checks:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Check", style="bold")
        table.add_column("Status")
        table.add_column("Your System")
        table.add_column("Required")
        table.add_column("Detail")

        for check in report.checks:
            status_style = {"ok": "[green]✓ OK[/green]",
                            "warning": "[yellow]⚠ Warning[/yellow]",
                            "error": "[red]✗ Error[/red]"}
            table.add_row(
                check.name,
                status_style.get(check.status, check.status),
                check.current,
                check.required,
                check.message,
            )
        console.print(table)

    # Overall verdict
    console.print()
    if report.compatible:
        console.print(
            "  [success]✓ System check passed — your machine meets the project's "
            "requirements.[/success]"
        )
    else:
        console.print(
            "  [error]✗ System check failed — your machine does not meet the "
            "project's requirements.[/error]"
            "\n  [muted]Fix the critical issues above and re-run, or override "
            "by continuing anyway.[/muted]"
        )
    console.print()
