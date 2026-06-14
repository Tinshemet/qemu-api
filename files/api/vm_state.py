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

VM_BASE_DIR = os.path.expanduser("~/.qemu_vms")
STATE_FILE  = os.path.join(VM_BASE_DIR, ".state.json")


class VMState:
    def __init__(self):
        os.makedirs(VM_BASE_DIR, exist_ok=True)
        self._data: Dict[str, Dict] = self._load()

    def _load(self) -> Dict:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self._data, f, indent=2)

    def set_running(self, name: str, pid: int):
        self._data[name] = {"pid": pid, "started": datetime.now().isoformat()}
        self._save()

    def set_stopped(self, name: str):
        self._data.pop(name, None)
        self._save()

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

    def poll(self):
        try:
            return None if self._proc.is_running() else 0
        except psutil.NoSuchProcess:
            return 1

    def terminate(self):
        try:
            self._proc.terminate()
        except psutil.NoSuchProcess:
            pass

    def kill(self):
        try:
            self._proc.kill()
        except psutil.NoSuchProcess:
            pass

    def is_running(self):
        try:
            return self._proc.is_running()
        except psutil.NoSuchProcess:
            return False
