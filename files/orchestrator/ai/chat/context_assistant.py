"""
context_assistant.py — Post-Inject AI Auditor

Runs after the AI produces a tool call, before execute_tool fires.
Checks two things only:
  1. Did the AI pick a tool that matches what the user asked for?
  2. Did the AI invent a value for a field the user never mentioned?

Returns a hint string injected as _INTERNAL_ into the message stream
so the AI can self-correct. Returns None if everything looks fine.

Never blocks execution directly — downstream layers (context gate,
preflight) remain the hard stops. This is a soft nudge, not a gate.
"""

import json
import os
import re
from typing import Dict, List, Optional, Set, Tuple

from orchestrator.executor_client import _VM_TOOLS

_here = os.path.dirname(__file__)
with open(os.path.join(_here, "context_assistant_config.json")) as _f:
    _CFG = json.load(_f)

# Derived from the canonical tool data (executor/command_catalog.py) — trigger
# words live WITH the tool, not in a separate config copy that drifts (this map
# used to miss fleet / run_guest_command / label tools). Guarded for
# orchestrator-only checkouts where executor/ is absent.
try:
    from executor.command_catalog import TOOL_TRIGGERS as _TOOL_HINTS
except ImportError:
    _TOOL_HINTS: Dict[str, List[str]] = _CFG.get("tool_hints", {})
_SLOT_PATTERNS:     Dict[str, List[str]] = _CFG["slot_patterns"]
# NOTE (single-source audit, 2026-07-17): this is a DIFFERENT concept from the
# tool registry's required-fields, so it is NOT derived from it. The registry's
# `req` = "fields the tool needs to RUN" (name + os_type for create_vm); THIS list
# = "fields that must be literally GROUNDED in the prompt or they're a
# hallucination" — a deliberately narrower set that EXCLUDES fields with sensible
# defaults (os_type defaults to linux, so an ungrounded os_type is a fine default,
# not a hallucination). Two distinct facts → two legitimate sources, not drift.
_REQUIRED_FIELDS:   Dict[str, List[str]] = _CFG["required_fields"]
_HIGH_STAKES:       Dict[str, List[str]] = _CFG["high_stakes_optional"]
_SPECIFICITY_RULES: List[Tuple[str, str]] = [tuple(r) for r in _CFG["specificity_rules"]]
_OPPOSING_PAIRS:    List[Tuple[str, str]] = [tuple(r) for r in _CFG["opposing_pairs"]]
_CONTRA_TRIGGERS:   Dict[str, List[str]] = _CFG["contradiction_triggers"]
_STOPWORDS:         set                  = set(_CFG["stopwords"])
_PROTECTED_NAMES:   set                  = set(_CFG["protected_names"])
_CONTRA_WINDOW:     int                  = _CFG["contradiction_window"]
_MSG:               Dict[str, str]       = _CFG["messages"]


def _strip_stopwords(text: str) -> str:
    """Drop corporate stopwords from a product string for token matching."""
    return " ".join(w for w in text.split() if w not in _STOPWORDS)


def _name_near_triggers(prompt: str, name: str, triggers: List[str]) -> bool:
    """Return True if ``name`` appears within ``_CONTRA_WINDOW`` words of any trigger.

    Args:
        prompt:   Raw user prompt string.
        name:     VM or entity name to look for.
        triggers: Trigger phrases (e.g. ``["delete", "remove"]``).

    Returns:
        ``True`` if ``name`` is found near a trigger word; ``False`` otherwise.

    Example::

        _name_near_triggers("please delete myvm now", "myvm", ["delete"])
        # → True
        _name_near_triggers("list all vms", "myvm", ["delete"])
        # → False
    """
    words  = prompt.split()
    name_positions = [i for i, w in enumerate(words) if name in w]
    if not name_positions:
        return False
    trigger_positions: List[int] = []
    for trigger in triggers:
        tw = trigger.split()
        for i in range(len(words) - len(tw) + 1):
            if words[i : i + len(tw)] == tw:
                trigger_positions.append(i)
    return any(
        abs(np - tp) <= _CONTRA_WINDOW
        for np in name_positions
        for tp in trigger_positions
    )


# ── Hint scanner ───────────────────────────────────────────────────────────────

# Plain "\b" word boundaries aren't enough here: a hyphen/underscore already
# counts as a non-word character, so r"\bbuild\b" still matches inside
# "build-box". A VM/snapshot name that happens to contain a trigger word as
# one of its hyphenated segments (e.g. name="build-box", trigger="build")
# must not be mistaken for the user invoking that trigger. Require that
# neither side of the match be a word character OR a dash/underscore.
# A "~" in a trigger opts that trigger into GAPPED matching: the segments must
# appear IN ORDER with up to _TRIGGER_GAP intervening words between them, instead
# of contiguously. This is how "put~on~network" catches "put probe on the new
# network" — real prompts interpose the VM name between the verb and "network",
# which a rigid contiguous phrase ("put vm on network") can never match. Opt-in
# per trigger, so the plain contiguous triggers keep their exactness.
_TRIGGER_GAP = 3


def _trigger_in(text: str, trigger: str) -> bool:
    """Return True if the trigger occurs in the text (contiguous, or gapped if '~')."""
    if "~" in trigger:
        segs = [re.escape(s.strip()) for s in trigger.split("~") if s.strip()]
        joiner = r"(?:\W+\w+){0,%d}\W+" % _TRIGGER_GAP
        pattern = r"(?<![\w-])" + joiner.join(segs) + r"(?![\w-])"
    else:
        pattern = r"(?<![\w-])" + re.escape(trigger) + r"(?![\w-])"
    return re.search(pattern, text) is not None


def scan_tool_hints(prompt: str) -> List[str]:
    """
    Returns the list of tool names the prompt hints at based on trigger words.
    Applies specificity rules so "delete snapshot" doesn't also hint delete_vm.
    Multiple results that survive de-duplication = genuinely conflicting intent.
    """
    lower   = prompt.lower()
    cleaned = _strip_stopwords(lower)
    hinted  = set()

    for tool, triggers in _TOOL_HINTS.items():
        if any(_trigger_in(lower, t) or _trigger_in(cleaned, t) for t in triggers):
            hinted.add(tool)

    # Remove generic hints suppressed by a more specific one
    for specific, generic in _SPECIFICITY_RULES:
        if specific in hinted and generic in hinted:
            hinted.discard(generic)

    return list(hinted)


# ── Slot extractor ─────────────────────────────────────────────────────────────

# A "name" capture is only trusted when we're confident the token really is a
# name. Two signals establish that: an ANCHORED pattern (called/named/quoted)
# whose anchor word explicitly announces the next token as a name, OR — for
# every other, unanchored name pattern (`vm X`, `on X`, `for X`, verb+X) — the
# token LOOKING like an identifier (contains a digit, dash, or underscore, e.g.
# probe9_min / dev-box). Real VM/entity names in this codebase always carry that
# shape; the false captures these bare patterns produce ("is vm running", "stop
# status", "make room for logs") never do. This anchored-exempt + shape-check-
# the-rest rule replaces what used to be an ever-growing 112-word denylist of
# English words (`protected_names`, now empty) — see project memory
# project-protected-names-heuristic. Listing the ANCHORED patterns (rather than
# the bare ones) also fails safe: if a pattern string ever drifts out of sync
# with the config, the token gets shape-checked (stricter) instead of silently
# re-opening the false-capture hole the way the old bare-list did.
_ANCHORED_NAME_PATTERNS = {
    r'\bcalled ([\w][\w\-_\.]*)',
    r'\bnamed ([\w][\w\-_\.]*)',
    r'"([\w][\w\-_\.]*)"',
    r"'([\w][\w\-_\.]*)'",
}
_IDENTIFIER_SHAPE = re.compile(r"[-_0-9]")


def extract_slots(prompt: str) -> Dict[str, Optional[str]]:
    """
    Scans the prompt for explicitly mentioned values.
    Returns None for a slot when the user did not mention it.
    Never infers — only literal matches.
    """
    lower = prompt.lower()
    slots: Dict[str, Optional[str]] = {}

    for field, patterns in _SLOT_PATTERNS.items():
        found = None
        for p in patterns:
            if any(c in p for c in r"\.+*?[](){}^$|"):
                # Try every match so a rejected first hit doesn't hide a valid later one
                for m in re.finditer(p, lower):
                    candidate = m.group(1)
                    if candidate in _STOPWORDS or candidate in _PROTECTED_NAMES:
                        continue
                    # Unanchored name captures must look like an identifier;
                    # anchored ones (called/named/quoted) are trusted as-is.
                    if (field == "name" and p not in _ANCHORED_NAME_PATTERNS
                            and not _IDENTIFIER_SHAPE.search(candidate)):
                        continue
                    found = candidate
                    break
            else:
                # plain keyword
                if p in lower:
                    found = p
            if found:
                break
        slots[field] = found

    return slots


# ── Proactive pre-pass (upstream, deterministic) ────────────────────────────────

# Always-offered safety net so tool-narrowing can NEVER trap the model: whatever
# the narrowed sub-goal set, it can always ask (clarify), ground (list_vms), or
# check capabilities (check_system).
_NARROW_CORE_TOOLS = frozenset({"clarify", "list_vms", "check_system"})


def narrow_tools(user_input: str, all_tools: List[dict]) -> List[dict]:
    """Deterministically narrow the offered tools to the sub-goal (hinted ∪ core).

    NOT USED in the main chat loop: VERIFIED to DEGRADE llama3.1 (2026-07-17) —
    offering 4 tools instead of 46 made "create X same OS as test1" hallucinate the
    os_type 4/4 runs, where the full tool set resolved it correctly 4/4. The fuller
    tool context anchors the weak model's reasoning; restricting it backfires. Kept
    for possible per-node experiments in the Score engine (where a node is a single
    narrow sub-goal, a different regime than a full-turn prompt).

    Returns the (hinted ∪ core) schemas when intent is clear, or ALL of them when the
    prompt is ambiguous (no confident hint) — never narrows on uncertainty.
    """
    try:
        hints = set(scan_tool_hints(user_input))
    except Exception:
        return all_tools
    if not hints:
        return all_tools
    keep = hints | _NARROW_CORE_TOOLS
    narrowed = [t for t in all_tools if t.get("function", {}).get("name") in keep]
    return narrowed or all_tools


def proactive_prep(user_input: str) -> str:
    """Deterministic guidance computed BEFORE the model acts, to cut the first
    wrong step (churn matters more once the Score runs many small steps).

    Reuses the same deterministic signals the post-hoc check uses — literal slot
    extraction + trigger-word tool hints — with NO model call and NO inference, so
    it contrasts a chaotic system rather than adding to it. Returns "" when there's
    nothing confident to add (never guesses). Meant to be injected transiently
    into the prompt, not persisted.

    >>> proactive_prep("create a ubuntu vm called dev")
    'GUIDANCE (grounded, deterministic — trust it): likely tool(s): create_vm; user specified: name=dev.'
    """
    try:
        hints = scan_tool_hints(user_input)
        slots = {k: v for k, v in extract_slots(user_input).items() if v}
    except Exception:
        return ""
    # The extractor can match one token to several slots (name / new_name /
    # snap_name for "called dev"); keep only the highest-priority slot per distinct
    # value so the guidance isn't noisy/misleading.
    _priority = ("name", "os_type", "network_mode", "new_name", "snap_name")
    _seen: set = set()
    _curated: Dict[str, str] = {}
    for k in _priority + tuple(k for k in slots if k not in _priority):
        v = slots.get(k)
        if v and v not in _seen:
            _curated[k] = v
            _seen.add(v)
    slots = _curated
    parts: List[str] = []
    if hints:
        parts.append("likely tool(s): " + ", ".join(sorted(hints)))
    if slots:
        parts.append("user specified: " + ", ".join(f"{k}={v}" for k, v in sorted(slots.items())))
    if not parts:
        return ""
    return "GUIDANCE (grounded, deterministic — trust it): " + "; ".join(parts) + "."


# ── Reconciler ─────────────────────────────────────────────────────────────────

def check_context(
    prompt:         str,
    tool_name:      str,
    args:           Dict,
    recent_context: str = "",
    known_names:    Optional[Set[str]] = None,
) -> Optional[str]:
    """
    Main entry point called from the chat loop after the AI produces a
    tool call but before execute_tool runs.

    recent_context: space-joined string of the last N real user messages.
    Used to verify field values in multi-turn flows where the entity was
    named in a prior turn and only a confirmation arrived as the current prompt.

    known_names: the actual set of existing VM names, if the caller has it handy.
    Ground truth, not a heuristic — when supplied, a tool that references an
    existing VM (see _VM_TOOLS) with a name outside this set is flagged
    unconditionally. Optional and defaults to None so this function stays a
    pure, network-free check for callers/tests that don't have a registry to
    pass (e.g. the layer7 test harness) — the check is simply skipped then.

    Returns a short _INTERNAL_ hint string if something looks wrong,
    or None if the call looks grounded and the tool matches intent.
    """
    # Recon / query tools are always valid as precursors to any action.
    # Never flag them as mismatches — the AI legitimately calls list_vms
    # before launching, scan_isos before creating, etc.
    _RECON_TOOLS = {
        "list_vms", "scan_isos", "check_system",
        "list_profiles", "list_networks", "monitor_all", "monitor_vm",
    }
    if tool_name in _RECON_TOOLS:
        return None

    slots  = extract_slots(prompt)
    # Extended slot check — also scan recent history so "yes" after
    # "delete test1" doesn't lose the name extracted from the earlier turn.
    _ext_slots = extract_slots(prompt + " " + recent_context) if recent_context else slots
    lower  = prompt.lower()

    # ── Check 4: contradictory intent ─────────────────────────────────────────
    # Runs first — if the prompt is self-contradictory, skip mismatch entirely
    # (mismatch would be a confusing second message about the same problem).
    # Uses _CONTRA_TRIGGERS (looser, no "vm" suffix required) and proximity so
    # "create dev-box and delete staging" is not flagged.
    # Checks both name and snap_name so snapshot create+delete is caught too.
    _contra_fired = False
    _targets = [v for v in [slots.get("name"), slots.get("snap_name"), args.get("name")]
                if v and isinstance(v, str)]
    for _target in _targets:
        # The same name must appear at least twice — once near each action.
        # A single occurrence can only be the object of one action, so it
        # can't be evidence of a contradiction (avoids short-sentence false positives).
        if lower.count(_target.lower()) < 2:
            continue
        for tool_a, tool_b in _OPPOSING_PAIRS:
            near_a = _name_near_triggers(lower, _target.lower(), _CONTRA_TRIGGERS.get(tool_a, []))
            near_b = _name_near_triggers(lower, _target.lower(), _CONTRA_TRIGGERS.get(tool_b, []))
            if near_a and near_b:
                return _MSG["contradictory_prompt"].format(
                    tool_a=tool_a, tool_b=tool_b, name=_target,
                )

    hints  = scan_tool_hints(prompt)
    issues = []

    # ── Check 1: tool mismatch ─────────────────────────────────────────────────
    # Only fire when we have a clear hint signal. No hints = ambiguous prompt,
    # don't second-guess the AI.
    if hints and tool_name not in hints:
        issues.append(_MSG["tool_mismatch"].format(hints=hints, tool_name=tool_name))

    # ── Check 2: hallucinated required fields ──────────────────────────────────
    # Only flag fields that are both required AND trackable (in slot_patterns).
    # Untracked optional fields (memory_mb, cpu_cores, etc.) are left alone —
    # those are safe creative defaults.
    # Uses _ext_slots (current prompt + recent history) so multi-turn flows
    # don't incorrectly flag a value named in a previous message.
    for field in _REQUIRED_FIELDS.get(tool_name, []):
        ai_value = args.get(field)
        if ai_value is None:
            continue                    # field absent — context gate handles this
        if field not in slots:
            continue                    # not a trackable slot — skip
        ai_str = str(ai_value).lower()
        value_in_prompt = ai_str in lower or (recent_context and ai_str in recent_context.lower())
        if _ext_slots[field] is None and not value_in_prompt:
            issues.append(_MSG["hallucinated_field"].format(field=field, value=repr(ai_value)))

    # ── Check 3: high-stakes optional fields ───────────────────────────────────
    # These are boolean/flag fields that are dangerous when set but unlikely
    # to appear as extractable slots in the prompt. Flag whenever the AI sets
    # them to a truthy value and the user gave no explicit signal.
    for field in _HIGH_STAKES.get(tool_name, []):
        ai_value = args.get(field)
        if not ai_value:
            continue
        user_mentioned = (
            field in slots and slots[field] is not None   # trackable and found
        )
        if not user_mentioned:
            issues.append(_MSG["high_stakes_field"].format(field=field, value=repr(ai_value)))

    # ── Check 5: unknown VM reference (ground truth, not a heuristic) ─────────
    # If the caller supplied the actual known-VM registry, a reference-style
    # tool (see _VM_TOOLS) naming a VM that doesn't exist is unambiguous — the
    # AI hallucinated or mistyped the name. "all" is a valid sentinel for
    # stop_vm (stop everything), not a real name, so it's exempted rather than
    # checked against the registry.
    if known_names is not None and tool_name in _VM_TOOLS:
        ref_name = args.get("name")
        # Compare case-insensitively — the pipeline resolves VM names that way
        # (active_library.resolve), so `Test1` against a registry `test1` is a
        # valid reference, not a hallucination worth a re-plan nudge.
        if (ref_name and ref_name != "all"
                and ref_name.lower() not in {k.lower() for k in known_names}):
            issues.append(_MSG["unknown_vm_reference"].format(
                name=ref_name, known=sorted(known_names),
            ))

    if not issues:
        return None

    return " | ".join(issues)
