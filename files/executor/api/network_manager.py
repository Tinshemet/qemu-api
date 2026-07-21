"""
network_manager.py — Network Isolation Layer

Creates private virtual networks between VMs using QEMU's socket
multicast networking. VMs on the same isolated net can talk to each
other but NOT to the internet.
"""

import json
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from .qemu_config import MachineConfig

_CFG  = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_DIRS = _CFG["dirs"]
_NET  = _CFG["network"]

VM_BASE_DIR      = os.path.expanduser(_DIRS["vm_base"])
ISOLATED_NET_DIR = os.path.join(VM_BASE_DIR, _DIRS["isolated_net"])

# Same vendor-OUI pool NetworkConfig._generate_mac draws from, so an isolated-net
# NIC looks as real as a VM's main NIC instead of announcing itself as QEMU.
_VENDOR_OUI_MAP = _CFG["vendor_oui_map"]
_ALL_OUIS       = [oui for ouis in _VENDOR_OUI_MAP.values() for oui in ouis]


class IsolatedNetManager:
    NET_FILE = os.path.join(ISOLATED_NET_DIR, "networks.json")

    def __init__(self):
        os.makedirs(ISOLATED_NET_DIR, exist_ok=True)
        self._nets: Dict[str, Dict] = self._load()

    # Reads the networks.json state file from disk.
    # In: nothing → Out: dict
    def _load(self) -> Dict:
        """Load the persisted network map, or an empty dict on error."""
        if os.path.exists(self.NET_FILE):
            try:
                with open(self.NET_FILE) as f:
                    return json.load(f)
            except Exception:
                pass  # corrupt/unreadable network state — start from empty rather than crash
        return {}

    # Writes the current network state back to networks.json.
    # In: nothing → Out: nothing
    def _save(self) -> None:
        """Persist the network map to disk."""
        with open(self.NET_FILE, "w") as f:
            json.dump(self._nets, f, indent=2)

    # Creates a named multicast network with an auto-assigned port.
    # In: str net_name → Out: dict with success and network info
    def create_network(self, net_name: str) -> Dict[str, Any]:
        """Create a named user network; return a result dict."""
        self._nets = self._load()   # reload-before-mutate (see IsolatedNetManager note)
        if net_name in self._nets:
            return {"success": False, "error": f"Network '{net_name}' already exists."}
        used_ports = [n["mcast_port"] for n in self._nets.values()]
        port = _NET["start_port"]
        while port in used_ports:
            port += 1
        self._nets[net_name] = {
            "name":       net_name,
            "mcast_port": port,
            "mcast_addr": _NET["mcast_addr"],
            "members":    [],
            "created":    datetime.now().isoformat(),
        }
        self._save()
        return {"success": True, "network": self._nets[net_name]}

    # Removes a network by name from state and disk.
    # In: str net_name → Out: dict with success
    def delete_network(self, net_name: str) -> Dict[str, Any]:
        """Delete a named network; return a result dict."""
        self._nets = self._load()   # reload-before-mutate
        if net_name not in self._nets:
            return {"success": False, "error": f"Network '{net_name}' not found."}
        del self._nets[net_name]
        self._save()
        return {"success": True, "message": f"Network '{net_name}' deleted."}

    # Returns all currently defined isolated networks.
    # In: nothing → Out: List[dict]
    def list_networks(self) -> List[Dict]:
        """Return all defined networks."""
        self._nets = self._load()   # reflect any out-of-process changes
        return list(self._nets.values())

    # Drops a VM from every network's member list — called by delete_vm so a
    # deleted VM doesn't linger as a phantom member (e.g. get counted, or have
    # its name reused by an unrelated future VM that then appears to already
    # belong to a network it was never added to).
    # In: str vm_name → Out: nothing
    def remove_vm_from_all_networks(self, vm_name: str) -> None:
        """Remove vm_name from every network's members list; no-op if absent."""
        self._nets = self._load()   # reload-before-mutate
        changed = False
        for net in self._nets.values():
            if vm_name in net["members"]:
                net["members"].remove(vm_name)
                changed = True
        if changed:
            self._save()

    # Generates a locally-unique MAC from a real vendor OUI — QEMU assigns the
    # same hardcoded default (52:54:00:12:34:56) to every unmarked
    # virtio-net-pci device, so every VM on an isolated net needs its own MAC
    # to avoid collision, but a raw "52:54:00:xx:xx:xx" MAC is itself a
    # hypervisor tell (QEMU's assigned OUI) to anything on the same L2
    # segment. Draw from the same real-vendor pool NetworkConfig._generate_mac
    # uses instead, optionally matched to the VM's own manufacturer_hint so
    # the isolated-net NIC looks consistent with its main NIC.
    # In: Optional[str] hint → Out: str
    @staticmethod
    def _random_mac(hint: Optional[str] = None) -> str:
        """Return a fresh real-vendor-OUI MAC for an isolated-net NIC."""
        pool = next(
            (ouis for key, ouis in _VENDOR_OUI_MAP.items() if key in (hint or "").lower()),
            _ALL_OUIS,
        )
        import random
        oui    = random.choice(pool)
        device = uuid.uuid4().bytes[:3]
        return oui + ":" + ":".join(f"{b:02x}" for b in device)

    # Returns the -netdev socket,mcast=... QEMU args to attach a VM to the network.
    # In: str net_name, str vm_name → Out: List[str] | None
    def get_netdev_args(self, net_name: str, vm_name: str) -> Optional[List[str]]:
        """Return QEMU -netdev args to attach a VM to an isolated network."""
        self._nets = self._load()   # reload-before-mutate (appends a member below)
        net = self._nets.get(net_name)
        if not net:
            return None
        if vm_name not in net["members"]:
            net["members"].append(vm_name)
            self._save()
        addr  = net["mcast_addr"]
        port  = net["mcast_port"]
        netid = f"iso_{net_name}"
        hint  = None
        try:
            src_cfg = MachineConfig.load(vm_name)
            hint = src_cfg.networks[0].manufacturer_hint if src_cfg.networks else None
        except FileNotFoundError:
            pass  # best-effort hint lookup — fall back to any real vendor OUI
        return [
            "-netdev", f"socket,id={netid},mcast={addr}:{port}",
            "-device", f"virtio-net-pci,netdev={netid},mac={self._random_mac(hint)}",
        ]

    # Appends the isolation network args to a stopped VM's extra_args and saves its config.
    # In: str net_name, str vm_name → Out: dict with success
    def add_vm_to_network(self, net_name: str, vm_name: str) -> Dict[str, Any]:
        """Update a stopped VM's config to include an isolated network interface."""
        self._nets = self._load()   # reload-before-mutate: another process may have changed state
        net = self._nets.get(net_name)
        if not net:
            return {"success": False, "error": f"Network '{net_name}' not found."}
        try:
            cfg = MachineConfig.load(vm_name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}
        netid = f"iso_{net_name}"
        # Idempotency keys on the netdev id, NOT on the individual arg strings:
        # the -device VALUE carries a fresh random MAC each call, so a per-arg
        # `not in` dedup never matches it and would append a second NIC with no
        # preceding -device flag (two devices bound to one netdev → broken launch).
        if any(netid in a for a in cfg.extra_args):
            if vm_name not in net["members"]:
                net["members"].append(vm_name)
                self._save()
            return {"success": True,
                    "message": f"VM '{vm_name}' is already on isolated network '{net_name}'."}
        addr  = net["mcast_addr"]
        port  = net["mcast_port"]
        hint  = cfg.networks[0].manufacturer_hint if cfg.networks else None
        # netid absent → append the full flag+value sequence as a unit (extend,
        # not the old per-arg loop, which also wrongly skipped the shared
        # -netdev/-device flag tokens when the VM was already on another net).
        cfg.extra_args.extend([
            "-netdev", f"socket,id={netid},mcast={addr}:{port}",
            "-device", f"virtio-net-pci,netdev={netid},mac={self._random_mac(hint)}",
        ])
        if vm_name not in net["members"]:
            net["members"].append(vm_name)
            self._save()
        cfg.save()
        return {"success": True, "message": f"VM '{vm_name}' added to isolated network '{net_name}'."}
