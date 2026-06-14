"""
network_manager.py — Network Isolation Layer

Creates private virtual networks between VMs using QEMU's socket
multicast networking. VMs on the same isolated net can talk to each
other but NOT to the internet.
"""

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from .qemu_config import MachineConfig

_CFG  = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_DIRS = _CFG["dirs"]
_NET  = _CFG["network"]

VM_BASE_DIR      = os.path.expanduser(_DIRS["vm_base"])
ISOLATED_NET_DIR = os.path.join(VM_BASE_DIR, _DIRS["isolated_net"])


class IsolatedNetManager:
    NET_FILE = os.path.join(ISOLATED_NET_DIR, "networks.json")

    def __init__(self):
        os.makedirs(ISOLATED_NET_DIR, exist_ok=True)
        self._nets: Dict[str, Dict] = self._load()

    # Reads the networks.json state file from disk.
    # In: nothing → Out: dict
    def _load(self) -> Dict:
        if os.path.exists(self.NET_FILE):
            try:
                with open(self.NET_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    # Writes the current network state back to networks.json.
    # In: nothing → Out: nothing
    def _save(self):
        with open(self.NET_FILE, "w") as f:
            json.dump(self._nets, f, indent=2)

    # Creates a named multicast network with an auto-assigned port.
    # In: str net_name → Out: dict with success and network info
    def create_network(self, net_name: str) -> Dict[str, Any]:
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
        if net_name not in self._nets:
            return {"success": False, "error": f"Network '{net_name}' not found."}
        del self._nets[net_name]
        self._save()
        return {"success": True, "message": f"Network '{net_name}' deleted."}

    # Returns all currently defined isolated networks.
    # In: nothing → Out: List[dict]
    def list_networks(self) -> List[Dict]:
        return list(self._nets.values())

    # Returns the -netdev socket,mcast=... QEMU args to attach a VM to the network.
    # In: str net_name, str vm_name → Out: List[str] | None
    def get_netdev_args(self, net_name: str, vm_name: str) -> Optional[List[str]]:
        """Return QEMU -netdev args to attach a VM to an isolated network."""
        net = self._nets.get(net_name)
        if not net:
            return None
        if vm_name not in net["members"]:
            net["members"].append(vm_name)
            self._save()
        addr  = net["mcast_addr"]
        port  = net["mcast_port"]
        netid = f"iso_{net_name}"
        return [
            "-netdev", f"socket,id={netid},mcast={addr}:{port}",
            "-device", f"virtio-net-pci,netdev={netid}",
        ]

    # Appends the isolation network args to a stopped VM's extra_args and saves its config.
    # In: str net_name, str vm_name → Out: dict with success
    def add_vm_to_network(self, net_name: str, vm_name: str) -> Dict[str, Any]:
        """Update a stopped VM's config to include an isolated network interface."""
        net = self._nets.get(net_name)
        if not net:
            return {"success": False, "error": f"Network '{net_name}' not found."}
        try:
            cfg = MachineConfig.load(vm_name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}
        addr  = net["mcast_addr"]
        port  = net["mcast_port"]
        netid = f"iso_{net_name}"
        iso_args = [
            "-netdev", f"socket,id={netid},mcast={addr}:{port}",
            "-device", f"virtio-net-pci,netdev={netid}",
        ]
        for arg in iso_args:
            if arg not in cfg.extra_args:
                cfg.extra_args.append(arg)
        if vm_name not in net["members"]:
            net["members"].append(vm_name)
            self._save()
        cfg.save()
        return {"success": True, "message": f"VM '{vm_name}' added to isolated network '{net_name}'."}
