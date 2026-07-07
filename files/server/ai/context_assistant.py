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
from typing import Dict, List, Optional, Tuple

_here = os.path.dirname(__file__)
with open(os.path.join(_here, "context_assistant_config.json")) as _f:
    _CFG = json.load(_f)

_TOOL_HINTS:        Dict[str, List[str]] = _CFG["tool_hints"]
_SLOT_PATTERNS:     Dict[str, List[str]] = _CFG["slot_patterns"]
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
def _trigger_in(text: str, trigger: str) -> bool:
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

# These two "name" patterns have no anchor word signaling "the next token is a
# name" — they fire on any "vm <word>" / "on <word>" substring, including status
# questions like "is vm running" or "turn on X". Unlike anchored patterns
# (called/named/quoted), a bare-pattern capture is only trusted if it looks like
# a real identifier (contains a digit, dash, or underscore — e.g. probe9_min,
# dev-box) rather than relying on an ever-growing denylist of English words.
_BARE_NAME_PATTERNS = {
    r"\bvm ([\w][\w\-_\.]*)",
    r"\bon ([\w][\w\-_\.]*)",
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
                    if p in _BARE_NAME_PATTERNS and not _IDENTIFIER_SHAPE.search(candidate):
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


# ── Reconciler ─────────────────────────────────────────────────────────────────

def check_context(
    prompt:         str,
    tool_name:      str,
    args:           Dict,
    recent_context: str = "",
) -> Optional[str]:
    """
    Main entry point called from the chat loop after the AI produces a
    tool call but before execute_tool runs.

    recent_context: space-joined string of the last N real user messages.
    Used to verify field values in multi-turn flows where the entity was
    named in a prior turn and only a confirmation arrived as the current prompt.

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

    if not issues:
        return None

    return " | ".join(issues)
