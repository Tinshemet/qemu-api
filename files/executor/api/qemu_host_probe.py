"""
qemu_host_probe.py — OVMF firmware auto-detection + host capability probing.

Extracted from qemu_config.py: detect_ovmf/OVMF/BIOS_OPTIONS and check_system_capabilities
(KVM, QEMU version, CPU flags, RAM, disk). qemu_config re-exports these for its importers.
"""
import json
import os
import platform
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

import psutil

with open(os.path.join(os.path.dirname(__file__), "config.json")) as _f:
    _CFG = json.load(_f)

# ─────────────────────────────────────────────
#  OVMF AUTO-DETECTION
#  Searches all known install locations across distros
# ─────────────────────────────────────────────

_OVMF_SEARCH_PATHS      = _CFG["ovmf_search_paths"]
_OVMF_VARS_SEARCH_PATHS = _CFG["ovmf_vars_search_paths"]
_OVMF_MS_CODE_PATHS     = _CFG["ovmf_ms_code_paths"]
_OVMF_MS_VARS_PATHS     = _CFG["ovmf_ms_vars_paths"]


# Scans a list of paths and returns the first one that exists on disk.
# In: List[str] paths → Out: str | None
def _find_first(paths: List[str]) -> Optional[str]:
    """Return the first path in ``paths`` that exists, or None."""
    for p in paths:
        if os.path.exists(p):
            return p
    return None


# Searches all known distro locations for OVMF firmware files.
# In: nothing → Out: dict with keys code, vars, ms_code, ms_vars, available
def detect_ovmf() -> Dict[str, Optional[str]]:
    """Auto-detect OVMF firmware paths on this system.

    Returns:
        Dict with keys ``code``, ``vars``, ``ms_code``, ``ms_vars``,
        and ``available`` (True only when both code and vars are found).

    Example::

        detect_ovmf()
        # → {"code": "/usr/share/OVMF/OVMF_CODE.fd",
        #    "vars": "/usr/share/OVMF/OVMF_VARS.fd",
        #    "ms_code": None, "ms_vars": None, "available": True}
    """
    code    = _find_first(_OVMF_SEARCH_PATHS)
    vars_   = _find_first(_OVMF_VARS_SEARCH_PATHS)
    ms_code = _find_first(_OVMF_MS_CODE_PATHS)
    ms_vars = _find_first(_OVMF_MS_VARS_PATHS)
    return {
        "code":      code,
        "vars":      vars_,
        "ms_code":   ms_code,
        "ms_vars":   ms_vars,
        "available": code is not None and vars_ is not None,
    }


# Run detection once at import time
OVMF = detect_ovmf()

# Dynamic BIOS_OPTIONS built from detected paths
BIOS_OPTIONS: Dict[str, Optional[str]] = {
    "seabios": None,
    "ovmf":    OVMF["code"],
    "ovmf_ms": OVMF["ms_code"] or OVMF["code"],  # fallback to plain OVMF if no secboot
}


# Probes the host for KVM, QEMU version, CPU flags, RAM, disk, and arch.
# In: nothing → Out: dict with full capability report
def check_system_capabilities() -> Dict[str, Any]:
    """
    Probe the host system for KVM, OVMF, QEMU version, CPU features, etc.
    Used by the AI to answer compatibility questions.
    """
    caps = {}

    # KVM — Linux-only hardware accelerator
    if sys.platform == "linux":
        caps["kvm_available"] = os.path.exists("/dev/kvm")
        caps["kvm_readable"]  = os.access("/dev/kvm", os.R_OK | os.W_OK)
    else:
        caps["kvm_available"] = False
        caps["kvm_readable"]  = False

    # QEMU
    qemu = shutil.which("qemu-system-x86_64")
    caps["qemu_installed"] = qemu is not None
    if qemu:
        try:
            r = subprocess.run([qemu, "--version"], capture_output=True, text=True)
            caps["qemu_version"] = r.stdout.split("\n")[0]
        except Exception:
            caps["qemu_version"] = "unknown"

    # ARM QEMU (for Pi emulation)
    caps["qemu_arm_installed"]    = shutil.which("qemu-system-aarch64") is not None
    caps["qemu_arm_v7_installed"] = shutil.which("qemu-system-arm") is not None

    # OVMF
    caps["ovmf"] = OVMF

    # CPU info — use platform module; cpu flags only available on Linux via /proc/cpuinfo
    try:
        caps["host_cpu"] = platform.processor() or "unknown"
        cpu_flags: List[str] = []
        if sys.platform == "linux":
            with open("/proc/cpuinfo") as f:
                cpuinfo = f.read()
            cpu_flags = next(
                (l.split(":")[1].strip().split() for l in cpuinfo.splitlines() if l.startswith("flags")), []
            )
        caps["cpu_flags"] = cpu_flags
        caps["vmx"]    = "vmx"    in cpu_flags   # Intel VT-x
        caps["svm"]    = "svm"    in cpu_flags   # AMD-V
        caps["avx2"]   = "avx2"   in cpu_flags
        caps["avx512"] = "avx512f" in cpu_flags
    except Exception:
        caps["host_cpu"] = "unknown"
        caps["vmx"] = caps["svm"] = caps["avx2"] = caps["avx512"] = False

    # Architecture — platform.machine() works on all OSes
    caps["host_arch"] = platform.machine() or "x86_64"

    # Memory — psutil works on Linux, macOS, and Windows
    try:
        caps["host_memory_mb"] = psutil.virtual_memory().total // (1024 * 1024)
    except Exception:
        caps["host_memory_mb"] = 0

    # CPU core count — os.cpu_count() works everywhere
    caps["host_cpu_cores"] = os.cpu_count() or 1

    # Disk space in home — shutil.disk_usage() works on all OSes
    try:
        usage = shutil.disk_usage(os.path.expanduser("~"))
        caps["home_free_gb"] = usage.free // (1024 ** 3)
    except Exception:
        caps["home_free_gb"] = 0

    return caps
