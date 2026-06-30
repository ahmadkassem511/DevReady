"""Tests for the pre-install hardware compatibility check.

Focus on the fixes that stop it from FALSELY blocking installs:
  * a passing mention of CUDA/NVIDIA must not mark a GPU as required;
  * low RAM / few CPU cores are warnings, not hard blocks;
  * the engine must not let a hardware mismatch poison _install_ok (which would
    skip the launch even after a clean install).
"""

from devready.environment import system_check as sc


# -- requirement extraction (regex) ------------------------------------------
def test_passing_cuda_mention_does_not_require_gpu():
    for text in (
        "Tested on an NVIDIA A100.",
        "CUDA is optional for acceleration.",
        "Install the CUDA toolkit if you have an NVIDIA GPU.",
        "Runs on CPU or GPU.",
    ):
        req = sc._extract_regex(text + " Additional description here.")
        # Either no requirements parsed, or GPU not marked required.
        assert req is None or not req.gpu_required, text
        assert req is None or not req.gpu_cuda_required, text


def test_explicit_cuda_requirement_is_detected():
    req = sc._extract_regex("This project requires a CUDA-capable GPU to run.")
    assert req is not None and req.gpu_required and req.gpu_cuda_required


def test_low_ram_is_a_warning_not_a_block():
    hw = sc.HardwareInfo(
        os_name="Windows 11", os_arch="x86_64", cpu_cores=8, cpu_model="x",
        ram_gb=8.0, disk_free_gb=200.0,
    )
    req = sc.SystemRequirements(ram_min_gb=16.0, source="regex")
    report = sc.check_compatibility(hw, req)
    assert report.compatible is True  # low RAM must NOT block
    ram = next(c for c in report.checks if c.name == "RAM")
    assert ram.status == "warning"


def test_few_cpu_cores_is_a_warning_not_a_block():
    hw = sc.HardwareInfo(
        os_name="Windows 11", os_arch="x86_64", cpu_cores=2, cpu_model="x",
        ram_gb=16.0, disk_free_gb=200.0,
    )
    req = sc.SystemRequirements(cpu_min_cores=8, source="regex")
    report = sc.check_compatibility(hw, req)
    assert report.compatible is True
    cpu = next(c for c in report.checks if c.name == "CPU Cores")
    assert cpu.status == "warning"


def test_missing_required_gpu_still_flags_incompatible():
    # A genuinely required GPU that's absent is still a hard mismatch (for CLI).
    hw = sc.HardwareInfo(
        os_name="Windows 11", os_arch="x86_64", cpu_cores=8, cpu_model="x",
        ram_gb=16.0, disk_free_gb=200.0, gpu_model=None,
    )
    req = sc.SystemRequirements(gpu_required=True, gpu_cuda_required=True, source="regex")
    report = sc.check_compatibility(hw, req)
    assert report.compatible is False


def test_cuda_capability_detection():
    assert sc._gpu_is_cuda_capable("NVIDIA GeForce RTX 4090") is True
    assert sc._gpu_is_cuda_capable("Intel UHD Graphics 620") is False
    assert sc._gpu_is_cuda_capable("AMD Radeon RX 6800") is False
    assert sc._gpu_is_cuda_capable("") is False
