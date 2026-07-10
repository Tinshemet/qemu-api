"""
qemu_manager.py — VM Orchestration Layer

QemuManager is the single public façade for all VM lifecycle operations:
create, clone, launch, stop, status, monitor, snapshots, disk resize,
resource limits, network, display, shell, config, and log analysis.

Delegates to five focused mixin classes; this file provides only the
shared state, private helpers, and __init__.
"""
import json
import os
import subprocess
import sys
from typing import Any, Dict, List

import psutil

from ._vm_constants  import VM_BASE_DIR
from ._vm_diagnostics import _VmDiagnosticsMixin
from ._vm_lifecycle  import _VmLifecycleMixin
from ._vm_monitoring import _VmMonitoringMixin
from ._vm_operations import _VmOperationsMixin
from ._vm_runtime    import _VmRuntimeMixin
from ._vm_stealth    import _VmStealthMixin
from .network_manager import IsolatedNetManager
from .vm_state        import VMState, _PsutilProcWrapper


class QemuManager(
    _VmStealthMixin,
    _VmOperationsMixin,
    _VmDiagnosticsMixin,
    _VmMonitoringMixin,
    _VmRuntimeMixin,
    _VmLifecycleMixin,
):
    """Public façade for all VM lifecycle operations."""

    def __init__(self) -> None:
        """Create the VM base dir, initialise state and net managers, reconnect to surviving VMs."""
        os.makedirs(VM_BASE_DIR, exist_ok=True)
        self._state:      VMState                       = VMState()
        self._procs:      Dict[str, subprocess.Popen]  = {}
        self._setup_srvs: Dict[str, tuple]              = {}  # name → (HTTPServer, port)
        self.iso_nets:    IsolatedNetManager            = IsolatedNetManager()
        self._reconnect_running()

    # ------------------------------------------------------------------
    # Reconnect
    # ------------------------------------------------------------------

    def _reconnect_running(self) -> None:
        """Attach _PsutilProcWrapper for each PID that survived a terminal restart."""
        for name, pid in self._state.all_running().items():
            try:
                p = psutil.Process(pid)
                self._procs[name] = _PsutilProcWrapper(p)
            except psutil.NoSuchProcess:
                self._state.set_stopped(name)

    # ------------------------------------------------------------------
    # Private helpers shared by all mixins via self
    # ------------------------------------------------------------------

    def _is_running(self, name: str) -> bool:
        """Return True if the named VM has a live, non-zombie QEMU process."""
        proc = self._procs.get(name)
        if proc:
            if hasattr(proc, "poll"):
                if proc.poll() is None:
                    return True
            else:
                try:
                    if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                        return True
                except Exception:
                    pass  # liveness probe raced a dying process — treat as not running
        pid = self._state.get_pid(name)
        if pid:
            try:
                p = psutil.Process(pid)
                if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                    self._procs[name] = _PsutilProcWrapper(p)
                    return True
            except psutil.NoSuchProcess:
                pass  # pid vanished during adoption — treat as not running
        self._procs.pop(name, None)
        self._state.set_stopped(name)
        pid = self._find_qemu_pid(name)
        if pid:
            try:
                p = psutil.Process(pid)
                if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                    self._procs[name] = _PsutilProcWrapper(p)
                    self._state.set_running(name, pid)
                    return True
            except psutil.NoSuchProcess:
                pass  # pid vanished during adoption — treat as not running
        return False

    def _used_ports(self, kind: str) -> List[int]:
        """Return already-assigned VNC or SPICE ports across all VM configs.

        Args:
            kind: ``"vnc"`` or ``"spice"``.
        """
        ports: List[int] = []
        for name in os.listdir(VM_BASE_DIR):
            if name.startswith("_"):
                continue
            cfg_path = os.path.join(VM_BASE_DIR, name, "config.json")
            if os.path.exists(cfg_path):
                try:
                    with open(cfg_path) as f:
                        data = json.load(f)
                    if kind == "vnc"   and data.get("vnc_port"):
                        ports.append(data["vnc_port"])
                    if kind == "spice" and data.get("spice_port"):
                        ports.append(data["spice_port"])
                except Exception:
                    pass  # unreadable/partial port file — skip it when collecting ports
        return ports

    def _apply_cpu_pinning(self, pid: int, cpus: List[int]) -> None:
        """Pin a process to specific host CPU cores via ``taskset`` (Linux only).

        Args:
            pid:  Target process ID.
            cpus: List of CPU core indices to pin to.
        """
        if sys.platform != "linux":
            return
        subprocess.run(
            ["taskset", "-cp", ",".join(map(str, cpus)), str(pid)],
            capture_output=True,
        )

    # ------------------------------------------------------------------
    # Isolated network pass-throughs
    # ------------------------------------------------------------------

    def create_network(self, net_name: str) -> Dict[str, Any]:
        """Create an isolated tap/bridge network."""
        return self.iso_nets.create_network(net_name)

    def delete_network(self, net_name: str) -> Dict[str, Any]:
        """Delete a named isolated network."""
        return self.iso_nets.delete_network(net_name)

    def list_networks(self) -> List[Dict]:
        """List all isolated networks."""
        return self.iso_nets.list_networks()

    def add_vm_to_network(self, net_name: str, vm_name: str) -> Dict[str, Any]:
        """Attach a VM to an isolated network."""
        return self.iso_nets.add_vm_to_network(net_name, vm_name)
