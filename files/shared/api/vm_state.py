"""
vm_state.py — VM State Persistence Layer

Persists running VM PIDs to ~/.qemu_vms/.state.json so the manager
can reconnect after a terminal restart. Also provides _PsutilProcWrapper
which makes a psutil.Process behave like subprocess.Popen.
"""

import json
import os
from datetime import datetime
from typing import Dict, Optional

import psutil

_CFG  = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_DIRS = _CFG["dirs"]

VM_BASE_DIR = os.path.expanduser(_DIRS["vm_base"])
STATE_FILE  = os.path.join(VM_BASE_DIR, _DIRS["state_file"])


class VMState:
    def __init__(self):
        os.makedirs(VM_BASE_DIR, exist_ok=True)
        self._data: Dict[str, Dict] = self._load()

    # Reads .state.json from disk; returns empty dict on failure.
    # In: nothing → Out: dict
    def _load(self) -> Dict:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    # Writes current state to .state.json.
    # In: nothing → Out: nothing
    def _save(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self._data, f, indent=2)

    # Records a VM as running with its PID and start timestamp.
    # In: str name, int pid → Out: nothing
    def set_running(self, name: str, pid: int):
        self._data[name] = {"pid": pid, "started": datetime.now().isoformat()}
        self._save()

    # Removes a VM from the running state and saves.
    # In: str name → Out: nothing
    def set_stopped(self, name: str):
        self._data.pop(name, None)
        self._save()

    # Returns the PID for a VM if the process is still alive; cleans up state if dead.
    # In: str name → Out: int | None
    def get_pid(self, name: str) -> Optional[int]:
        entry = self._data.get(name)
        if not entry:
            return None
        pid = entry.get("pid")
        try:
            p = psutil.Process(pid)
            if p.is_running() and p.name().startswith("qemu"):
                return pid
        except (psutil.NoSuchProcess, TypeError):
            self.set_stopped(name)
        return None

    # Returns a {name: pid} dict of VMs whose processes are actually still alive.
    # In: nothing → Out: dict
    def all_running(self) -> Dict[str, int]:
        """Return {name: pid} for all VMs that are actually still running."""
        live = {}
        for name in list(self._data.keys()):
            pid = self.get_pid(name)
            if pid:
                live[name] = pid
        return live


class _PsutilProcWrapper:
    """Makes a psutil.Process behave like subprocess.Popen."""

    def __init__(self, proc: psutil.Process):
        self._proc = proc
        self.pid   = proc.pid

    # Returns None if the process is alive (like Popen.poll()), or 1 if it exited.
    # In: nothing → Out: int | None
    def poll(self):
        try:
            return None if self._proc.is_running() else 0
        except psutil.NoSuchProcess:
            return 1

    # Sends SIGTERM; silently ignores if the process is already gone.
    # In: nothing → Out: nothing
    def terminate(self):
        try:
            self._proc.terminate()
        except psutil.NoSuchProcess:
            pass

    # Sends SIGKILL; silently ignores if the process is already gone.
    # In: nothing → Out: nothing
    def kill(self):
        try:
            self._proc.kill()
        except psutil.NoSuchProcess:
            pass

    # Returns whether the wrapped process is still alive.
    # In: nothing → Out: bool
    def is_running(self):
        try:
            return self._proc.is_running() and self._proc.status() != "zombie"
        except psutil.NoSuchProcess:
            return False
