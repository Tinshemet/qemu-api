"""
stealth_persona.py — Stealth fingerprint-data generators.

Pure helpers that manufacture believable hardware identity values for a stealth
VM: a plausible BIOS version, per-unit RAM/CPU variance, a disk model string, a
real-machine persona pick, and a vendor-plausible serial. Kept together and
apart from the tool dispatch because they are self-contained and drive off a
private RNG so create_vm never perturbs the global `random` stream.

tool_executor.py's _execute_create_vm imports these back. This module imports
nothing from tool_executor — the edge is one-directional.
"""

import os
import random

from executor.api.qemu_config import get_all_profiles


# Dedicated RNG for all stealth randomness (persona, serial, disk model, BIOS/
# RAM/CPU variance) so create_vm never perturbs the global `random` state that
# callers and the test suite depend on — global-RNG pollution from library code
# makes downstream randomness order-dependent.
_STEALTH_RNG = random.Random()

# ── Stealth persona rotation ──────────────────────────────────────────────────
# Machine classes that are NOT plausible "real computer" personas (Raspberry Pi,
# bare 'minimal' configs) — excluded from the random stealth-persona pool.
_STEALTH_EXCLUDED_CLASSES = {"custom", ""}

# Real SATA SSD model strings — a stealth disk (SATA ide-hd, which supports
# model=) reports one of these so lsblk / inxi -D / smartctl show a believable
# drive instead of "QEMU NVMe Ctrl" (NVMe's model is a fixed, unspoofable tell)
# or a virtio /dev/vd* device.
_STEALTH_DISK_MODELS = [
    "Samsung SSD 870 EVO 500GB", "Samsung SSD 860 EVO 1TB", "Samsung SSD 870 QVO 1TB",
    "CT500MX500SSD1", "CT1000MX500SSD1", "WDC WDS500G2B0A-00SM50",
    "KINGSTON SA400S37480G", "SanDisk SDSSDH3-1T00G", "SK hynix SC401 SATA 512GB",
    "INTEL SSDSC2KW256G8", "Micron_1100_MTFDDAK512TBN", "TOSHIBA THNSNK256GVN8",
]


def _plausible_bios_version(manufacturer: str) -> str:
    """Return a vendor-plausible BIOS version for a user-named model not in the
    library — so a coherent firmware version can be synthesised rather than
    borrowed from an unrelated profile.

    Example::

        _plausible_bios_version("Dell Inc.")  # → "1.17.0"
    """
    return f"1.{_STEALTH_RNG.randint(4, 28)}.{_STEALTH_RNG.randint(0, 9)}"


def _apply_within_model_variance(cfg, profile_data: dict, args: dict) -> None:
    """Vary the fields that real units of a model actually differ on — BIOS
    version, installed RAM, CPU core count — within that model's real option
    set, so no two stealth VMs of the same model are byte-identical (beyond the
    already-unique serial and MAC).

    Values stay coherent because they are drawn from the profile's curated
    option lists (real configs that model shipped in), never generated blindly.
    Only fields the caller did not set explicitly are touched.

    Args:
        cfg:          MachineConfig being built (mutated in place).
        profile_data: The applied profile's dict (may carry ``bios_versions``,
                      ``memory_options_mb``, ``cpu_variants``).
        args:         Raw create_vm args — an explicit value here is never overridden.
    """
    import os
    import psutil
    pd = profile_data or {}
    if pd.get("bios_versions") and not args.get("bios_version"):
        cfg.bios_version = _STEALTH_RNG.choice(pd["bios_versions"])
    # RAM: pick from the model's real options, but never exceed ~50% of host RAM.
    # A guest claiming more memory than the host has thrashes and won't boot.
    if pd.get("memory_options_mb") and not args.get("memory_mb"):
        try:
            host_mb = psutil.virtual_memory().total // (1024 * 1024)
        except Exception:
            host_mb = 8192
        cap  = max(2048, int(host_mb * 0.5))
        opts = [m for m in pd["memory_options_mb"] if m <= cap] or [min(pd["memory_options_mb"])]
        cfg.memory_mb = min(_STEALTH_RNG.choice(opts), cap)
    # CPU: cpu_variants are [cores, total_threads]; -smp wants threads PER CORE,
    # so convert (else [4, 8] became 4*8 = 32 vCPUs). Cap total vCPUs to host CPUs
    # to avoid heavy oversubscription.
    if pd.get("cpu_variants") and not args.get("cpu_cores"):
        cores, total = _STEALTH_RNG.choice(pd["cpu_variants"])
        tpc       = max(1, total // cores)
        host_cpus = os.cpu_count() or 4
        if cores * tpc > host_cpus:
            cores = max(1, host_cpus // tpc)
        cfg.cpu_cores, cfg.cpu_threads = cores, tpc


def _generate_disk_model() -> str:
    """Pick a random real SATA SSD model string for a stealth VM's disk, so the
    drive reads as believable hardware to lsblk / inxi -D / smartctl."""
    return _STEALTH_RNG.choice(_STEALTH_DISK_MODELS)


def _pick_stealth_persona(form_factor: str = "", os_type: str = "") -> str:
    """Pick a random realistic hardware-profile name for a stealth VM.

    Assigning a fresh persona per VM is what lets rotation defeat long-term
    fingerprinting: each stealth VM presents as a different real machine rather
    than one recognisable identity.

    Args:
        form_factor: Optional constraint — "laptop", "desktop", or "server".
                     Empty / "any" / "random" picks across all form factors.
        os_type:     Guest OS — skips personas that pin a different OS (e.g. a
                     Mac mini persona is not handed to a Linux guest).

    Returns:
        A profile name from ``get_all_profiles()``, or "" if none qualify.

    Example::

        _pick_stealth_persona()          # → "office_laptop"  (random each call)
        _pick_stealth_persona("server")  # → "hpe_proliant_dl380"
    """
    ff = (form_factor or "").lower().strip()
    if ff in ("", "any", "random"):
        ff = ""
    guest_os = (os_type or "").lower().strip()
    pool = []
    for name, pd in get_all_profiles().items():
        mc = (pd.get("machine_class") or "").lower()
        if mc in _STEALTH_EXCLUDED_CLASSES:
            continue
        if not pd.get("manufacturer") or not pd.get("product_name"):
            continue
        p_os = (pd.get("os_type") or "").lower()
        if p_os and guest_os and p_os != guest_os:
            continue   # don't present macOS hardware under a Linux/Windows guest
        if ff and mc != ff:
            continue
        pool.append(name)
    return _STEALTH_RNG.choice(pool) if pool else ""


def _generate_stealth_serial(manufacturer: str) -> str:
    """Generate a realistic per-unit serial number in the vendor's format.

    Unique serials per VM are essential for rotation — two VMs of the same model
    must never share a serial (an obvious fingerprinting giveaway).

    Args:
        manufacturer: Vendor name (e.g. "Dell Inc.", "Lenovo", "HP").

    Returns:
        A serial string shaped like that vendor's real serials.

    Example::

        _generate_stealth_serial("Dell Inc.")  # → "7X9QRT2"    (7-char service tag)
        _generate_stealth_serial("HP")         # → "412CD9A7BK" (10-char)
    """
    import string
    m      = (manufacturer or "").lower()
    upper  = string.ascii_uppercase
    alnum  = string.ascii_uppercase + string.digits
    digits = string.digits
    pick   = lambda alpha, n: "".join(_STEALTH_RNG.choice(alpha) for _ in range(n))
    if "dell" in m:
        return pick(alnum, 7)                                     # Dell service tag
    if "lenovo" in m:
        return pick(upper, 2) + pick(alnum, 6)                    # Lenovo: 2 letters + 6
    if "hp" in m or "hewlett" in m or "proliant" in m:
        return pick(digits, 3) + pick(upper, 2) + pick(alnum, 5)  # HP: 10 chars
    if "apple" in m:
        return "C02" + pick(alnum, 8)                            # Apple 11-char
    if "supermicro" in m:
        return "S" + pick(digits, 9)                             # Supermicro
    return pick(alnum, 8)                                        # generic fallback

