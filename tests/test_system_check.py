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


# -- GPU requirement detection (the SkyReels-style "genuinely needs a GPU") ----
def test_stated_vram_implies_gpu_required():
    # The exact phrasing SkyReels-V2 uses — no literal "requires CUDA" anywhere.
    text = (
        "## Inference\n"
        "Single-GPU & Multi-GPU Inference Code.\n"
        'pipeline = pipeline.to("cuda")\n'
        "Generating a 540P video using the 1.3B model requires approximately "
        "14.7GB peak VRAM, while the 14B model demands around 51.2GB peak VRAM.\n"
    )
    gpu, cuda, vram = sc._detect_gpu_requirement(text.lower())
    assert gpu is True
    assert cuda is True
    assert vram == 14.7  # smallest stated figure (entry barrier)


def test_gpu_inference_phrasing_requires_gpu():
    gpu, _cuda, _vram = sc._detect_gpu_requirement("supports single-gpu inference.")
    assert gpu is True


def test_cuda_install_instructions_require_cuda():
    text = "install with: pip install torch --index-url https://download.pytorch.org/whl/cu121 and nvidia driver 535"
    gpu, cuda, _vram = sc._detect_gpu_requirement(text.lower())
    assert gpu is True and cuda is True


def test_gpu_optional_statement_overrides():
    # Even with cuda code present, an explicit CPU/optional statement wins.
    for text in (
        'runs on cpu. you can also use .to("cuda") if you have a gpu.',
        "no gpu required — works on cpu.",
        "gpu is optional; cpu is supported.",
    ):
        gpu, cuda, _vram = sc._detect_gpu_requirement(text)
        assert gpu is False, text
        assert cuda is False, text


def test_merge_ors_gpu_flags():
    # LLM missed the GPU requirement; regex caught it -> merged must keep it.
    llm = sc.SystemRequirements(ram_min_gb=16.0, source="llm")
    rgx = sc.SystemRequirements(gpu_required=True, gpu_cuda_required=True,
                                gpu_vram_min_gb=14.7, source="regex")
    merged = sc._merge_requirements(llm, rgx)
    assert merged.gpu_required is True
    assert merged.gpu_cuda_required is True
    assert merged.gpu_vram_min_gb == 14.7
    assert merged.ram_min_gb == 16.0


def test_merge_takes_smaller_minimums():
    a = sc.SystemRequirements(ram_min_gb=16.0, disk_min_gb=50.0)
    b = sc.SystemRequirements(ram_min_gb=8.0, disk_min_gb=100.0)
    merged = sc._merge_requirements(a, b)
    assert merged.ram_min_gb == 8.0   # least aggressive warning
    assert merged.disk_min_gb == 50.0


def test_integrated_gpu_detection():
    assert sc._gpu_is_integrated("Intel(R) HD Graphics 3000") is True
    assert sc._gpu_is_integrated("Intel(R) UHD Graphics 620") is True
    assert sc._gpu_is_integrated("AMD Radeon(TM) Graphics") is True   # Ryzen APU iGPU
    assert sc._gpu_is_integrated("Apple M2") is True
    assert sc._gpu_is_integrated("NVIDIA GeForce RTX 4090") is False  # discrete
    assert sc._gpu_is_integrated("AMD Radeon RX 6800 XT") is False    # discrete
    assert sc._gpu_is_integrated(None) is False


def test_integrated_gpu_does_not_satisfy_gpu_requirement():
    """The user's exact case: AI returned only gpu_required (no CUDA flag), and
    an integrated Intel HD GPU wrongly 'passed'. It must now fail."""
    hw = sc.HardwareInfo(
        os_name="Windows 10", os_arch="amd64", cpu_cores=4, cpu_model="i5",
        ram_gb=7.2, disk_free_gb=12.8,
        gpu_model="Intel(R) HD Graphics 3000", gpu_vram_gb=2.1, gpu_cuda_capable=False,
    )
    req = sc.SystemRequirements(gpu_required=True, source="llm")  # note: cuda flag NOT set
    report = sc.check_compatibility(hw, req)
    assert report.compatible is False
    gpu = next(c for c in report.checks if c.name == "GPU")
    assert gpu.status == "error"
    assert "integrated" in gpu.message.lower()


def test_discrete_non_cuda_gpu_satisfies_plain_gpu_requirement():
    """A discrete AMD card (ROCm/DirectML-capable) should satisfy a plain
    'needs a GPU' requirement that doesn't specifically demand CUDA."""
    hw = sc.HardwareInfo(
        os_name="Linux", os_arch="x86_64", cpu_cores=16, cpu_model="Ryzen",
        ram_gb=32.0, disk_free_gb=500.0,
        gpu_model="AMD Radeon RX 6800 XT", gpu_cuda_capable=False,
    )
    req = sc.SystemRequirements(gpu_required=True, source="regex")  # not cuda-required
    report = sc.check_compatibility(hw, req)
    assert report.compatible is True
    gpu = next(c for c in report.checks if c.name == "GPU")
    assert gpu.status == "ok"


def test_skyreels_style_gpu_repo_is_incompatible_on_non_cuda_machine():
    """End-to-end: a GPU/VRAM repo must be flagged on a CUDA-less machine
    (the bug: it silently 'passed')."""
    text = (
        "Single-GPU & Multi-GPU Inference. The 1.3B model requires approximately "
        "14.7GB peak VRAM.\n"
        'pipe = pipe.to("cuda")\n'
    )
    req = sc._extract_regex(text)
    assert req is not None and req.gpu_required and req.gpu_cuda_required
    hw = sc.HardwareInfo(
        os_name="Windows 10", os_arch="x86_64", cpu_cores=4, cpu_model="x",
        ram_gb=7.0, disk_free_gb=100.0,
        gpu_model="Intel(R) HD Graphics 3000", gpu_cuda_capable=False,
    )
    report = sc.check_compatibility(hw, req)
    assert report.compatible is False
    gpu = next(c for c in report.checks if c.name == "GPU")
    assert gpu.status == "error"
