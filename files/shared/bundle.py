"""
bundle.py — the agent bundle: a self-contained ~/.gorgon/_agents/<name>/ folder.

Centralizes "where an agent's pieces live" — the .grgn contract, missions, claim
findings, tool-reliability stats, the per-agent skin, and per-agent commands — so no
caller reaches for dirname(__file__) or a scattered ~/.gorgon path. The built-in
doorman stays code-resident as the fail-closed fallback; a bundle is checked first.

Path-only: reads/writes go through each piece's owner (contract.py, mission.py,
findings_store.py, the skin/command loaders) using these locations. AGENTS_ROOT is a
module attribute (not a bound import) so a test isolates every bundle path by
patching it once.
"""

import glob
import os

from shared.config import AGENTS_DIR

# The bundle root (~/.gorgon/_agents). A module attribute so tests patch it once.
AGENTS_ROOT = AGENTS_DIR


class Bundle:
    """The on-disk home of one agent, keyed by its name (== ``active_agent_key``).

    ``<name>.grgn`` is the contract; ``missions/`` its sealed missions; ``findings``
    /``toolstats`` its per-agent learning; ``skin.json`` its appearance overrides;
    ``commands/`` its own command definitions.
    """

    def __init__(self, name: str):
        self.name = name

    @property
    def path(self) -> str:
        return os.path.join(AGENTS_ROOT, self.name)

    @property
    def contract_path(self) -> str:
        return os.path.join(self.path, f"{self.name}.grgn")

    @property
    def sig_path(self) -> str:
        return self.contract_path + ".sig"

    @property
    def missions_dir(self) -> str:
        return os.path.join(self.path, "missions")

    @property
    def findings_path(self) -> str:
        return os.path.join(self.path, "findings.json")

    @property
    def toolstats_path(self) -> str:
        return os.path.join(self.path, "toolstats.json")

    @property
    def reliability_path(self) -> str:
        return os.path.join(self.path, "reliability.json")

    @property
    def skin_path(self) -> str:
        return os.path.join(self.path, "skin.json")

    @property
    def commands_path(self) -> str:
        return os.path.join(self.path, "commands.json")

    @property
    def commands_dir(self) -> str:
        return os.path.join(self.path, "commands")

    def exists(self) -> bool:
        return os.path.isdir(self.path)

    def has_contract(self) -> bool:
        return os.path.isfile(self.contract_path)

    def ensure(self) -> "Bundle":
        """Create the bundle folder (and its missions/ subdir) if absent."""
        os.makedirs(self.missions_dir, exist_ok=True)
        return self


def list_bundles() -> list:
    """Every agent that has a bundle folder, by name (sorted)."""
    if not os.path.isdir(AGENTS_ROOT):
        return []
    return sorted(n for n in os.listdir(AGENTS_ROOT)
                  if os.path.isdir(os.path.join(AGENTS_ROOT, n)))


def resolve_grgn(name_or_file: str, code_dir: str = None) -> str:
    """The .grgn path for an agent selection (the single resolution authority).

    An absolute path is returned as-is; otherwise the bundle contract
    (~/.gorgon/_agents/<name>/<name>.grgn) wins if it exists, falling back to
    ``<code_dir>/<name_or_file>`` — where the built-in doorman and any not-yet-migrated
    agent live. The NAME is the selection's basename without extension.
    """
    if os.path.isabs(name_or_file):
        return name_or_file
    name = os.path.splitext(os.path.basename(name_or_file))[0]
    b = Bundle(name)
    if b.has_contract():
        return b.contract_path
    return os.path.join(code_dir, name_or_file) if code_dir else b.contract_path


def migrate(code_dir: str = None) -> list:
    """One-time, idempotent migration of legacy scattered agent state into bundles.

    Moves each agent's forged code-dir ``.grgn`` (+ ``.sig``), its
    ``~/.gorgon/missions/<name>/``, and its ``~/.gorgon/{findings,toolstats}.<name>.json``
    into ``~/.gorgon/_agents/<name>/``. The built-in doorman stays code-resident.
    Safe to call repeatedly — an agent already present in its bundle is skipped.
    Returns the names touched. (The bundle-first resolver means an unmigrated agent
    still loads from the code dir, so this is consolidation, not a correctness gate.)
    """
    import shutil
    legacy = os.path.expanduser("~/.gorgon")
    touched = set()

    # 1. forged code-dir .grgn (doorman stays code-resident)
    if code_dir and os.path.isdir(code_dir):
        for f in glob.glob(os.path.join(code_dir, "*.grgn")):
            name = os.path.splitext(os.path.basename(f))[0]
            b = Bundle(name)
            if name == "doorman" or b.has_contract():
                continue
            b.ensure()
            shutil.move(f, b.contract_path)
            if os.path.isfile(f + ".sig"):
                shutil.move(f + ".sig", b.sig_path)
            touched.add(name)

    # 2. legacy per-agent missions folders (per-file move: ensure() may have already
    # created an empty missions/, so guard on the file, not the dir)
    legacy_missions = os.path.join(legacy, "missions")
    if os.path.isdir(legacy_missions):
        for name in os.listdir(legacy_missions):
            src = os.path.join(legacy_missions, name)
            if not os.path.isdir(src):
                continue
            b = Bundle(name)
            b.ensure()
            for mf in os.listdir(src):
                dst = os.path.join(b.missions_dir, mf)
                if os.path.exists(dst):
                    continue
                shutil.move(os.path.join(src, mf), dst)
                touched.add(name)
            try:
                os.rmdir(src)          # drop the now-empty legacy folder
            except OSError:
                pass

    # 3. legacy claim + tool-reliability files
    for prefix, dest in (("findings", "findings_path"), ("toolstats", "toolstats_path")):
        for f in glob.glob(os.path.join(legacy, f"{prefix}.*.json")):
            name = os.path.basename(f)[len(prefix) + 1:-len(".json")]
            b    = Bundle(name)
            if os.path.isfile(getattr(b, dest)):
                continue
            b.ensure()
            shutil.move(f, getattr(b, dest))
            touched.add(name)

    return sorted(touched)


def list_agent_grgns(code_dir: str = None) -> list:
    """Every agent .grgn path: bundle contracts first, then the code-resident
    templates (doorman, …) not shadowed by a bundle of the same name."""
    paths, seen = [], set()
    for name in list_bundles():
        b = Bundle(name)
        if b.has_contract():
            paths.append(b.contract_path)
            seen.add(name)
    if code_dir and os.path.isdir(code_dir):
        for f in sorted(glob.glob(os.path.join(code_dir, "*.grgn"))):
            if os.path.splitext(os.path.basename(f))[0] not in seen:
                paths.append(f)
    return paths
