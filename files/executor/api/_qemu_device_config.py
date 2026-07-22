"""
_qemu_device_config.py — DiskConfig + NetworkConfig dataclasses.

Extracted from qemu_config.py to keep it focused on MachineConfig. Re-exported from
qemu_config so existing `from .qemu_config import DiskConfig, NetworkConfig` importers work.
"""
import json
import os
import random
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

with open(os.path.join(os.path.dirname(__file__), "config.json")) as _f:
    _CFG = json.load(_f)
_DC = _CFG["disk_config_defaults"]
_NC = _CFG["network_config_defaults"]

# ─────────────────────────────────────────────
#  DISK CONFIG
# ─────────────────────────────────────────────

@dataclass
class DiskConfig:
    path:       str
    size_gb:    int  = _DC["size_gb"]
    format:     str  = _DC["format"]
    bus:        str  = _DC["bus"]
    cache:      str  = _DC["cache"]
    discard:    bool = _DC["discard"]
    ssd:        bool = _DC["ssd"]
    boot:       bool = _DC["boot"]
    disk_model: str  = ""

    # Coerces size_gb to int — guards against AI sending strings like "60".
    # In: self (post-construction) → Out: nothing (self-mutation)
    def __post_init__(self) -> None:
        # Coerce string values from AI (it sometimes sends "60" instead of 60)
        self.size_gb = int(self.size_gb)

    # Converts this disk config into -drive / -device QEMU args for its bus type.
    # In: int index → Out: List[str]
    def to_qemu_args(self, index: int = 0) -> List[str]:
        """Return the QEMU -drive/-device args for this disk."""
        drive_id = f"drive{index}"
        args = [
            "-drive",
            f"file={self.path},"
            f"format={self.format},"
            f"id={drive_id},"
            f"cache={self.cache},"
            f"if=none"
            + (",discard=unmap" if self.discard else ""),
        ]
        if self.bus == "nvme":
            model_suffix = f",model={self.disk_model}" if self.disk_model else ""
            args += ["-device", f"nvme,drive={drive_id},serial=nvme{index}{model_suffix}"]
        elif self.bus == "virtio":
            ssd_hint = ",rotation_rate=1" if self.ssd else ""
            args += ["-device", f"virtio-blk-pci,drive={drive_id}{ssd_hint}"]
        elif self.bus == "scsi":
            product_suffix = f",product={self.disk_model}" if self.disk_model else ""
            args += ["-device", f"scsi-hd,drive={drive_id}{product_suffix}"]
        elif self.bus == "sata":
            model_suffix = f",model={self.disk_model}" if self.disk_model else ""
            # q35 uses ICH9-AHCI — the controller is added by QemuArgBuilder._disks()
            args += ["-device", f"ide-hd,drive={drive_id},bus=ahci.{index}{model_suffix}"]
        else:
            # ide fallback — only works on non-q35 machines
            model_suffix = f",model={self.disk_model}" if self.disk_model else ""
            args = [
                "-drive",
                f"file={self.path},format={self.format},if=ide,cache={self.cache}"
                + (",discard=unmap" if self.discard else "")
                + model_suffix,
            ]
        return args


# ─────────────────────────────────────────────
#  NETWORK CONFIG
# ─────────────────────────────────────────────

@dataclass
class NetworkConfig:
    mode:              str           = _NC["mode"]
    model:             str           = _NC["model"]
    mac:               Optional[str] = None
    bridge:            str           = _NC["bridge"]
    ip:                Optional[str] = None
    hostname:          Optional[str] = None
    port_forwards:     List[tuple]   = field(default_factory=list)
    manufacturer_hint: Optional[str] = None
    slirp_subnet:      Optional[str] = None   # stealth NAT: e.g. "192.168.1.0/24"

    # Generates or validates the MAC address on init.
    # In: self (post-construction) → Out: nothing (self-mutation)
    def __post_init__(self) -> None:
        if not self.mac:
            self._generate_mac()
        else:
            # Validate and fix incoming MAC — must be exactly 6 octets
            self.mac = self._fix_mac(self.mac)

    # OUIs keyed by normalized manufacturer keyword — used to pick a vendor-consistent MAC.
    _VENDOR_OUI_MAP = _CFG["vendor_oui_map"]
    _ALL_OUIS       = [oui for ouis in _CFG["vendor_oui_map"].values() for oui in ouis]

    # Generates a MAC using a vendor-matched OUI when possible, otherwise any real OUI.
    # In: nothing → Out: sets self.mac
    def _generate_mac(self) -> None:
        """Assign a stable, locally-administered MAC to this NIC if unset."""
        import random
        hint = (self.manufacturer_hint or "").lower()
        pool = next(
            (ouis for key, ouis in self._VENDOR_OUI_MAP.items() if key in hint),
            self._ALL_OUIS,
        )
        oui = random.choice(pool)
        device = uuid.uuid4().bytes[:3]
        self.mac = oui + ":" + ":".join(f"{b:02X}" for b in device)

    # Validates or salvages a MAC string; generates a fresh one if unfixable.
    # In: str → Out: str
    @staticmethod
    def _fix_mac(mac: str) -> str:
        """Validate MAC; return it unchanged if valid, else generate a new one.

        Args:
            mac: MAC address string to validate.

        Returns:
            The input MAC if it matches ``XX:XX:XX:XX:XX:XX``, otherwise a
            freshly generated random MAC.

        Example::

            NetworkConfig._fix_mac("AA:BB:CC:DD:EE:FF")
            # → "AA:BB:CC:DD:EE:FF"
            NetworkConfig._fix_mac("not-a-mac")
            # → "52:54:00:xx:xx:xx"  (random)
        """
        import re
        mac = mac.strip()
        if re.match(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$", mac):
            return mac
        # Try to salvage — take first 6 hex pairs
        parts = re.findall(r"[0-9a-fA-F]{2}", mac.replace(":","").replace("-",""))
        if len(parts) >= 6:
            return ":".join(parts[:6])
        # Give up — generate fresh MAC using a real vendor OUI
        import random
        oui = random.choice(NetworkConfig._VENDOR_OUIS)
        device = uuid.uuid4().bytes[:3]
        return oui + ":" + ":".join(f"{b:02X}" for b in device)

    # Returns -netdev/-device args for NAT or bridge networking.
    # In: nothing → Out: List[str]
    def to_qemu_args(self) -> List[str]:
        """Return the QEMU -netdev/-device args for this network."""
        args = []
        if self.mode == "none":
            args += ["-nic", "none"]
            return args
        if self.mode == "nat":
            fwd = ""
            for hport, gport, proto in self.port_forwards:
                fwd += f",hostfwd={proto}::{hport}-:{gport}"
            slirp = ""
            if self.slirp_subnet:
                # Replace QEMU's tell-tale default 10.0.2.0/24 (gateway .2, guest
                # .15) with a home-router-looking subnet so the guest's own IP
                # config doesn't betray user-mode NAT. gateway=.1, DHCP pool=.100.
                try:
                    import ipaddress
                    _net  = ipaddress.ip_network(self.slirp_subnet, strict=False)
                    _gw   = _net.network_address + 1
                    _dhcp = _net.network_address + 100
                    slirp = f",net={_net.with_prefixlen},host={_gw},dhcpstart={_dhcp}"
                except ValueError:
                    slirp = ""
            args += [
                "-netdev", f"user,id=net0{fwd}{slirp}",
                "-device", f"{self.model},netdev=net0,mac={self.mac}",
            ]
        elif self.mode == "bridge":
            args += [
                "-netdev", f"bridge,id=net0,br={self.bridge}",
                "-device", f"{self.model},netdev=net0,mac={self.mac}",
            ]
        return args
