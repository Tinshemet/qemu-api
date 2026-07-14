"""
label_registry.py — universal user-defined VM label registry.

Labels (e.g. "work_vm", "test_vm") are free-form user tags assigned to VMs.
Like profiles, the SET of known label names is saved to the machine
(~/.qemu_vms/_labels.json) so labels can be referenced across many VMs with a
canonical, typo-free name. Per-VM assignment lives on MachineConfig.labels;
this module owns only the universal registry of label NAMES.
"""
import json
import os
from typing import List

_REGISTRY = os.path.expanduser("~/.qemu_vms/_labels.json")


# Reads the label registry file from disk (empty list if absent/corrupt).
# In: nothing → Out: List[str]
def _load() -> List[str]:
    """Load the registered label names, or [] on missing/corrupt state."""
    if os.path.exists(_REGISTRY):
        try:
            with open(_REGISTRY) as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            pass  # corrupt registry — start empty rather than crash
    return []


# Writes a sorted, de-duplicated label set back to the registry file.
# In: List[str] labels → Out: nothing
def _save(labels: List[str]) -> None:
    """Persist the label registry (sorted + de-duplicated)."""
    os.makedirs(os.path.dirname(_REGISTRY), exist_ok=True)
    with open(_REGISTRY, "w") as f:
        json.dump(sorted(set(labels)), f, indent=2)


# Returns all universally-registered label names.
# In: nothing → Out: List[str]
def list_registered_labels() -> List[str]:
    """Return every label name known to the machine."""
    return _load()


# Adds a label name to the universal registry if not already present.
# In: str label → Out: nothing
def register_label(label: str) -> None:
    """Register a label name so it can be referenced across VMs."""
    label = (label or "").strip()
    if not label:
        return
    labels = _load()
    if label not in labels:
        labels.append(label)
        _save(labels)


# Removes a label name from the universal registry.
# In: str label → Out: nothing
def unregister_label(label: str) -> None:
    """Drop a label name from the universal registry."""
    _save([l for l in _load() if l != label])
