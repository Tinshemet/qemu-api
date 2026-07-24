"""
command_catalog.py — loader + derived views for the command/tool registry.

All registry DATA lives in command_catalog.json (data as data — one file the whole
platform reads). This module loads it and exposes the derived views consumers
import (KNOWN_TOOLS, TOOL_EFFECTS, …), plus the drift asserts that fail LOUD at
import if the data ever references a tool that isn't in the registry.

The catalog drives BOTH help surfaces:
  - the terminal help (`gorgon help`): command + args + description, filtered to
    the tools the executor currently allows.
  - the AI-chat CLI help: the same, plus a short example prompt per command.

Each command entry (command_catalog.json "commands"):
  command     verb typed in the terminal ("launch"); "" for AI-only capabilities.
  tools       executor tool name(s) this maps to; used to filter the list against
              the allowed-tools list. Empty => client-side op, always shown.
  args        argument syntax shown after the verb.
  desc        one line: what it does / how it works.
  related     alias / trigger words (also fed to the shortcut matcher).
  ai_example  a short natural-language prompt for the AI (shown only in CLI help).
  category    grouping header.
  feature     non-standard VM parameter this command requires (e.g. "stealth"), or None.
  terminal    False for AI-only capabilities that have no terminal verb.

Keep command_catalog.json in sync with the dispatch tables in client/cli/commands.py
and orchestrator/ai/chat/commands/ — it is the authored list they should both render.
"""
import json
import os
from typing import Any, Dict, List

_DATA_PATH = os.path.join(os.path.dirname(__file__), "command_catalog.json")
_DATA = json.load(open(_DATA_PATH))

# User-facing command list (both help surfaces). Authored in command_catalog.json.
COMMAND_CATALOG: List[Dict[str, Any]] = _DATA["commands"]

# Header order for grouping in the rendered help.
CATEGORY_ORDER: List[str] = _DATA["category_order"]

# ── CANONICAL TOOL REGISTRY (single source of truth for the tool regime) ─────────
# The tool FACTS (req/vm/effect/rev), loaded from command_catalog.json. Everything
# that used to keep its own hand-maintained copy (server _KNOWN_TOOLS,
# executor_client _VM_TOOLS, active_library _TOOL_EFFECTS, tool_executor
# _REVERT_AWARE_TOOLS, the gate's required-fields) DERIVES from this via the
# accessors below — add a tool in the JSON once and every consumer updates. effect
# is restored to a tuple (JSON has no tuples); None = read-only. Keys MUST match
# tool_executor._run's dispatch exactly (asserted by tests/test_tool_registry.py).
# A tool's RISK and confirmation TIER are NOT here: those are a per-contract
# judgment in the .grgn agent files (orchestrator/ai/*.grgn) that reference these
# tools by name.
TOOL_SPECS: Dict[str, Dict[str, Any]] = {
    name: {
        "req":    list(s["req"]),
        "vm":     s["vm"],
        "effect": tuple(s["effect"]) if s.get("effect") else None,
        "rev":    s["rev"],
    }
    for name, s in _DATA["tools"].items()
}

# Derived views — consumers import THESE, never hand-maintained copies.
KNOWN_TOOLS:      frozenset       = frozenset(TOOL_SPECS)
VM_SCOPED_TOOLS:  frozenset       = frozenset(t for t, s in TOOL_SPECS.items() if s["vm"])
REVERT_TOOLS:     frozenset       = frozenset(t for t, s in TOOL_SPECS.items() if s["rev"])
TOOL_EFFECTS:     Dict[str, Any]  = {t: s["effect"] for t, s in TOOL_SPECS.items() if s["effect"]}
REQUIRED_FIELDS:  Dict[str, list] = {t: s["req"] for t, s in TOOL_SPECS.items() if s["req"]}

# Which arg names the VM a tool's effect targets (default "name"; clone writes the
# NEW vm). Tool metadata → lives WITH the tool data; imported by the Active Library.
TOOL_NAME_ARG:    Dict[str, str]  = _DATA["tool_name_arg"]

# Single-source link, enforced: TOOL_SPECS is THE authority for which tools exist;
# COMMAND_CATALOG only REFERENCES them. A command may name only registry tools
# (client-side commands use tools:[]). Fails LOUD at import if the catalog ever
# drifts from the registry — you can't reference a tool that isn't real.
_unknown_refs = {t for e in COMMAND_CATALOG for t in e.get("tools", []) if t not in TOOL_SPECS}
assert not _unknown_refs, f"command_catalog references non-registry tools: {sorted(_unknown_refs)}"

# Trigger words per TOOL, derived from each command's `related` words mapped
# through its tools — the single source for the context-assistant's tool hints
# (so adding a command's alias updates the assistant automatically).
def tool_trigger_words() -> Dict[str, list]:
    """command `related` words → per-tool trigger lists (derived, not hand-kept)."""
    out: Dict[str, list] = {}
    for e in COMMAND_CATALOG:
        words = e.get("related") or []
        for t in (e.get("tools") or []):
            out.setdefault(t, [])
            for w in words:
                if w not in out[t]:
                    out[t].append(w)
    return out


# Per-tool intent-detection triggers (context-assistant scan_tool_hints). Two sources,
# MERGED so a tool is tagged if EITHER knows it: the curated static list in
# command_catalog.json (hand-authored richness — 'spin up', 'provision', …) UNION the
# words DERIVED from each command's `related` aliases (tool_trigger_words). The static
# map had silently DRIFTED — add_label / delete_vm / the fleet-ping vocabulary lost
# their triggers — so a static-only source left whole tools un-hintable. Deriving from
# the command catalog closes that: adding a command alias now auto-tags its tools.
# Gapped ('~') triggers match words in order with a small gap (see _trigger_in).
_static_triggers:  Dict[str, List[str]] = _DATA["tool_triggers"]
_derived_triggers: Dict[str, List[str]] = tool_trigger_words()
TOOL_TRIGGERS: Dict[str, List[str]] = {}
for _t in set(_static_triggers) | set(_derived_triggers):
    _merged = list(_static_triggers.get(_t) or [])
    for _w in (_derived_triggers.get(_t) or []):
        if _w not in _merged:
            _merged.append(_w)          # static first (richness), derived fills gaps
    if _merged:
        TOOL_TRIGGERS[_t] = _merged
_bad_trig = [t for t in TOOL_TRIGGERS if t not in TOOL_SPECS]
assert not _bad_trig, f"TOOL_TRIGGERS references non-registry tools: {_bad_trig}"

# Create→attach relationships: once an entity of `kind` has been CREATED earlier in
# a plan, a later step that references it should ATTACH to the existing one, not
# re-create it. The Score engine reads this to make per-node tool selection
# LEDGER-AWARE: after `creator` ran, a node mentioning `keyword` (or the created
# name) is offered `attach` and NOT the creator. `name_arg` is the creator arg
# holding the created entity's name (so the node can be matched by that name too).
POST_CREATE_ATTACH: Dict[str, Dict[str, str]] = _DATA["post_create_attach"]
_bad_pca = [t for t in POST_CREATE_ATTACH if t not in TOOL_SPECS] + \
           [v["attach"] for v in POST_CREATE_ATTACH.values() if v["attach"] not in TOOL_SPECS]
assert not _bad_pca, f"POST_CREATE_ATTACH references non-registry tools: {_bad_pca}"
